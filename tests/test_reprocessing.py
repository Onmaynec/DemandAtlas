from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.db import SessionLocal
from backend.app.main import app
from backend.app.models import (
    DailyCoverage, DailySalesMart, DatasetState, PipelineRun, Product,
    QualityFinding, Source, StagedSale, Store
)
from backend.app.pipeline import ingest
from backend.app.reprocessing import ReplaceInput, replace_range

client = TestClient(app)
ENGINEER = {"Authorization": "Bearer demo-engineer"}
ANALYST = {"Authorization": "Bearer demo-analyst"}


def catalog():
    with SessionLocal.begin() as db:
        db.add(Store(id="S001", name="First", region="North"))
        db.add(Store(id="S002", name="Second", region="North"))
        db.add(Product(sku="SKU1", name="Soda", category="Food",
                       unit_cost=5, min_order=1, pack_size=10, lead_days=7))
        db.add(Source(id="source", kind="csv", entity="sales", config={}))


def sale(day: date, transaction: str, qty: int = 4, store: str = "S001"):
    return {
        "date": day.isoformat(), "store_id": store, "sku": "SKU1",
        "transaction_id": transaction, "line_id": "1",
        "qty": qty, "unit_price": "10.00", "unit_cost": "5.00",
    }


def version():
    with SessionLocal() as db:
        return db.scalar(select(DatasetState.version).where(DatasetState.id == 1))


def snapshot(day):
    with SessionLocal() as db:
        return db.scalar(select(DailySalesMart).where(
            DailySalesMart.date == day, DailySalesMart.store_id == "S001",
            DailySalesMart.sku == "SKU1"
        ))


def replace(day, rows, expected=1, key="replace-1"):
    return ReplaceInput(
        idempotencyKey=key, expectedDatasetVersion=expected,
        start=day, end=day, storeId="S001",
        reason="Corrections from audited shop report", rows=rows,
    )


def test_unchanged_replay_keeps_version_and_changed_fact_rejected():
    catalog()
    day = date.today() - timedelta(days=10)
    good = sale(day, "tx-1")
    first = ingest("source", "first", [good])
    assert first.dataset_version == 1
    replay = ingest("source", "retry-different-key", [good])
    assert replay.status == "published"
    assert version() == 1
    changed = ingest("source", "conflicting", [sale(day, "tx-1", qty=99)])
    assert changed.status == "rejected"
    assert changed.rows_rejected == 1
    assert version() == 1
    assert snapshot(day).units == 4
    with SessionLocal() as db:
        finding = db.scalar(select(QualityFinding).where(
            QualityFinding.run_id == changed.id))
        assert finding.rule == "changed_published_fact"
        assert finding.row_number == 2
    assert ingest("source", "conflicting", [sale(day, "tx-1", qty=99)]).id == changed.id


def test_replace_snapshot_is_atomic_idempotent_and_version_checked():
    catalog()
    day = date.today() - timedelta(days=10)
    original = ingest("source", "first", [sale(day, "tx-1"), sale(day, "tx-2", 6)])
    assert original.status == "published"
    replacement = replace(day, [sale(day, "tx-1", 25)], expected=1)
    run = replace_range("source", replacement)
    assert run["status"] == "published" and run["datasetVersion"] == 2
    assert snapshot(day).units == 25
    with SessionLocal() as db:
        staged = db.scalars(select(StagedSale)).all()
        assert [(s.transaction_id, s.qty) for s in staged] == [("tx-1", 25)]
    retry = replace_range("source", replacement)
    assert retry["runId"] == run["runId"]
    assert version() == 2
    with pytest.raises(RuntimeError, match="Stale dataset version"):
        replace_range("source", replace(day, [], expected=1, key="stale"))
    assert version() == 2
    zero = replace_range("source", replace(day, [], expected=2, key="zero"))
    assert zero["datasetVersion"] == 3
    assert snapshot(day) is None
    with SessionLocal() as db:
        assert db.scalar(select(DailyCoverage).where(
            DailyCoverage.date == day, DailyCoverage.store_id == "S001")) is not None


def test_invalid_reprocess_retains_previous_mart_and_exposes_line_numbers():
    catalog()
    day = date.today() - timedelta(days=10)
    ingest("source", "first", [sale(day, "tx-1")])
    data = [sale(day, "tx-1", -7), sale(day, "tx-2", 5, store="S002")]
    run = replace_range("source", replace(day, data))
    assert run["status"] == "rejected"
    assert run["rowsRejected"] == 2
    assert snapshot(day).units == 4
    assert version() == 1
    with SessionLocal() as db:
        findings = db.scalars(select(QualityFinding).where(
            QualityFinding.run_id == run["runId"])).all()
        assert {f.rule for f in findings} >= {"negative_qty", "outside_replacement_scope"}
        assert {f.row_number for f in findings} == {2, 3}


def test_transaction_identity_cannot_move_outside_partition():
    catalog()
    day = date.today() - timedelta(days=10)
    ingest("source", "first", [sale(day, "tx-1", store="S002")])
    result = replace_range("source", replace(day, [sale(day, "tx-1")]))
    assert result["status"] == "rejected"
    with SessionLocal() as db:
        assert db.scalar(select(StagedSale).where(StagedSale.transaction_id == "tx-1")).store_id == "S002"


def test_api_permissions_and_bad_version_return_403_409():
    catalog()
    day = date.today() - timedelta(days=10)
    ingest("source", "first", [sale(day, "tx-1")])
    payload = replace(day, [sale(day, "tx-1", 7)], expected=0).model_dump(mode="json")
    url = "/api/v1/pipelines/source/reprocess"
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, headers=ANALYST, json=payload).status_code == 403
    stale = client.post(url, headers=ENGINEER, json=payload)
    assert stale.status_code == 409, stale.text
    payload["expectedDatasetVersion"] = 1
    ok = client.post(url, headers=ENGINEER, json=payload)
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "published"
