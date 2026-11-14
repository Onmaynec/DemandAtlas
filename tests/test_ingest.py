from datetime import date, timedelta
from sqlalchemy import select
from backend.app.db import SessionLocal
from backend.app.models import DailySalesMart, Product, Source, Store, RawSale, QualityFinding
from backend.app.pipeline import ingest


def setup_catalog():
    with SessionLocal.begin() as db:
        db.add(Store(id="S001", name="One", region="North"))
        db.add(Product(sku="SKU00001", name="Product", category="Food",
                       unit_cost=10, min_order=1, pack_size=10, lead_days=7))
        db.add(Source(id="demo_sales", kind="csv", entity="sales", config={}))


def sale(i, qty=5, sku="SKU00001"):
    return {"date": (date.today() - timedelta(days=80 - i)).isoformat(),
            "store_id": "S001", "sku": sku,
            "transaction_id": f"sale-{i}", "line_id": "1",
            "qty": qty, "unit_price": "20.00", "unit_cost": "10.00"}


def test_retry_idempotence_and_atomic_quality_gate():
    setup_catalog()
    good = [sale(0), sale(1)]
    first = ingest("demo_sales", "request-1", good)
    assert first.status == "published"
    second = ingest("demo_sales", "request-1", good)
    assert first.id == second.id
    with SessionLocal() as db:
        original = list(db.scalars(select(DailySalesMart).order_by(DailySalesMart.date)))
        assert sum(x.units for x in original) == 10
    bad = [sale(2, qty=-4), sale(3, sku="missing")]
    rejected = ingest("demo_sales", "request-2", bad)
    assert rejected.status == "rejected" and rejected.rows_rejected == 2
    with SessionLocal() as db:
        assert sum(x.units for x in db.scalars(select(DailySalesMart))) == 10
        assert db.query(RawSale).filter(RawSale.run_id == rejected.id).count() == 2
        assert db.query(QualityFinding).filter(QualityFinding.run_id == rejected.id).count() >= 2


def test_distinct_idempotency_key_does_not_double_count():
    setup_catalog()
    rows = [sale(0)]
    ingest("demo_sales", "a", rows)
    ingest("demo_sales", "b", rows)
    with SessionLocal() as db:
        assert db.scalar(select(DailySalesMart.units)) == 5


def test_duplicates_within_csv_are_rejected():
    setup_catalog()
    run = ingest("demo_sales", "bad-dup", [sale(1), sale(1)])
    assert run.status == "rejected"
