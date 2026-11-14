"""Acceptance regressions for invalid batch and coverage visibility."""
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.db import SessionLocal
from backend.app.main import app
from backend.app.models import DatasetState, Product, Source, Store
from backend.app.pipeline import ingest

client = TestClient(app)
ANALYST = {"Authorization": "Bearer demo-analyst"}


def setup_catalog():
    with SessionLocal.begin() as db:
        db.add(Store(id="S001", name="First", region="North"))
        db.add(Store(id="S002", name="Second", region="North"))
        db.add(Product(sku="SKU1", name="Food", category="Food",
                       unit_cost=5, min_order=1, pack_size=10, lead_days=7))
        db.add(Source(id="source", kind="csv", entity="sales", config={}))


def sale(day, idx, store="S001", sku="SKU1", qty=1):
    return {"date": day.isoformat(), "store_id": store, "sku": sku,
            "transaction_id": f"T-{idx}", "line_id": "1",
            "qty": qty, "unit_price": "10", "unit_cost": "5"}


def test_at03_one_hundred_invalid_rows_cannot_damage_mart():
    setup_catalog()
    day = date.today() - timedelta(days=4)
    ingest("source", "valid", [sale(day, 0)])
    invalid = [sale(day, i+1, sku="NOT_A_PRODUCT" if i % 2 else "SKU1",
                    qty=1 if i % 2 else -1) for i in range(100)]
    result = ingest("source", "invalid", invalid)
    assert result.status == "rejected"
    assert result.rows_rejected == 100
    with SessionLocal() as db:
        assert db.scalar(select(DatasetState.version)) == 1
    response = client.get(
        f"/api/v1/metrics/sales?start={day}&end={day}&store=S001",
        headers=ANALYST,
    )
    assert response.status_code == 200
    assert response.json()["rows"][0]["units"] == 1


def test_at04_missing_store_three_days_must_show_incomplete_coverage():
    setup_catalog()
    start = date.today() - timedelta(days=3)
    end = date.today()
    ingest("source", "observed", [
        sale(start, 1), sale(start + timedelta(days=1), 2),
        sale(start + timedelta(days=2), 3), sale(end, 4),
        sale(start, 5, store="S002"),
    ])
    response = client.get(
        f"/api/v1/metrics/sales?start={start}&end={end}&group_by=store",
        headers=ANALYST,
    )
    assert response.status_code == 200
    coverage = response.json()["coverage"]
    assert coverage["expectedStoreDays"] == 8
    assert coverage["observedStoreDays"] == 5
    assert coverage["incomplete"] is True
    assert coverage["storeCoveragePct"] == 62.5



def test_coverage_detail_reports_only_permitted_store_days():
    setup_catalog()
    start = date.today() - timedelta(days=3)
    end = date.today()
    ingest("source", "observed", [sale(start, 1, store="S001")])
    details = client.get(
        f"/api/v1/metrics/coverage?start={start}&end={end}&store=S001",
        headers=ANALYST,
    )
    assert details.status_code == 200
    result = details.json()
    assert result["summary"]["expectedStoreDays"] == 4
    assert result["summary"]["observedStoreDays"] == 1
    assert len(result["missing"]) == 3
    observer = {"Authorization": "Bearer demo-observer"}
    forbidden = client.get(
        f"/api/v1/metrics/coverage?start={start}&end={end}&store=S999",
        headers=observer,
    )
    assert forbidden.status_code == 403
