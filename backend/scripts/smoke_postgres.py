"""Real PostgreSQL smoke: migration, ingestion races and snapshot corrections.

Called by Compose CI after demo seed. Uses separate source to avoid
interfering with the regular demo and asserts values from database reads.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from backend.app.db import SessionLocal
from backend.app.models import DatasetState, DailySalesMart, PipelineRun, Source
from backend.app.pipeline import ingest
from backend.app.reprocessing import ReplaceInput, replace_range


def run():
    source_id = "ci_" + uuid4().hex[:12]
    day = date.today() - timedelta(days=15)
    with SessionLocal.begin() as db:
        assert db.scalar(select(DatasetState.version)) is not None, "run migration 0002"
        db.add(Source(id=source_id, kind="csv", entity="sales", config={}))
    sale = {
        "date": day.isoformat(), "store_id": "S001",
        "sku": "SKU00001", "transaction_id": source_id,
        "line_id": "1", "qty": 4, "unit_price": "10.00", "unit_cost": "5.00"
    }
    with SessionLocal() as db:
        base = db.scalar(select(func.sum(DailySalesMart.units)).where(
            DailySalesMart.date == day, DailySalesMart.store_id == "S001",
            DailySalesMart.sku == "SKU00001"
        )) or 0
    # Concurrent retries must resolve to one immutable raw batch. Two API
    # clients can submit the same idempotency key at the same time.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ingest, source_id, "same-request", [sale]) for _ in range(2)]
        runs = [f.result() for f in futures]
    assert runs[0].id == runs[1].id, "Duplicate idempotency key produced two runs"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(PipelineRun).where(
            PipelineRun.source_id == source_id
        )) == 1
        assert db.scalar(select(func.sum(DailySalesMart.units)).where(
            DailySalesMart.date == day, DailySalesMart.store_id == "S001",
            DailySalesMart.sku == "SKU00001"
        )) == base + 4
        expected_version = db.scalar(select(DatasetState.version))
    rejected = ingest(source_id, "different-key", [{**sale, "qty": 99}])
    assert rejected.status == "rejected"
    with SessionLocal() as db:
        assert db.scalar(select(DatasetState.version)) == expected_version
    body = ReplaceInput(
        idempotencyKey="authoritative-snapshot", expectedDatasetVersion=expected_version,
        start=day, end=day, storeId="S001",
        reason="Verified correction in PostgreSQL smoke test",
        rows=[{**sale, "qty": 7}],
    )
    correction = replace_range(source_id, body)
    assert correction["status"] == "published"
    assert correction["datasetVersion"] == expected_version + 1
    assert replace_range(source_id, body)["runId"] == correction["runId"]
    with SessionLocal() as db:
        assert db.scalar(select(func.sum(DailySalesMart.units)).where(
            DailySalesMart.date == day, DailySalesMart.store_id == "S001",
            DailySalesMart.sku == "SKU00001"
        )) == base + 7
    print("PostgreSQL smoke passed: concurrency, conflicts, version, reprocessing")


if __name__ == "__main__":
    run()
