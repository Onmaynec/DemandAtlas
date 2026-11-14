"""Deterministic synthetic data generator, no personal identifiers."""
import argparse
import csv
import os
import random
from datetime import date, timedelta
from sqlalchemy import insert
from backend.app.db import SessionLocal
from backend.app.models import Inventory, Product, Source, Store
from backend.app.pipeline import ingest


def create_catalog(seed=42):
    rnd = random.Random(seed)
    with SessionLocal.begin() as db:
        if db.get(Store, "S001"):
            return
        db.execute(insert(Store), [
            {"id": f"S{i:03d}", "name": f"Магазин {i:02d}",
             "region": ("Север", "Юг", "Восток", "Запад", "Центр")[(i - 1) % 5]}
            for i in range(1, 26)
        ])
        db.execute(insert(Product), [
            {"sku": f"SKU{i:05d}", "name": f"Товар {i:05d}",
             "category": ("Food", "Home", "Electronics", "Clothing", "Health")[(i - 1) % 5],
             "unit_cost": round(15 + (i % 90) * 3.1, 2),
             "min_order": 5 if i % 3 else 10, "pack_size": 10 if i % 7 else 5,
             "lead_days": 7 if i % 4 else 14} for i in range(1, 5001)
        ])
        db.execute(insert(Inventory), [
            {"store_id": f"S{s:03d}", "sku": f"SKU{i:05d}",
             "on_hand": rnd.randrange(5, 70), "inbound": rnd.randrange(0, 12)}
            for s in range(1, 26) for i in range(1, 201)
        ])
        db.add_all([Source(id="demo_sales", kind="csv", entity="sales", config={}),
                    Source(id="demo_stock", kind="csv", entity="stock", config={})])


def generate_rows(total: int, seed: int):
    rnd = random.Random(seed)
    today = date.today()
    first = today - timedelta(days=729)
    for i in range(total):
        if i < 120:
            day, store, sku = today - timedelta(days=119 - i), "S001", "SKU00001"
            qty = 6 + (i % 7) * 2
        else:
            day = first + timedelta(days=rnd.randrange(730))
            store = f"S{rnd.randrange(1, 26):03d}"
            sku = f"SKU{rnd.randrange(1, 5001):05d}"
            qty = max(0, int(rnd.gauss(8, 4)))
        yield {
            "date": day.isoformat(), "store_id": store, "sku": sku,
            "transaction_id": f"T{i:09d}", "line_id": "1",
            "qty": qty, "unit_price": str(round(30 + (i % 70) * 2.2, 2)),
            "unit_cost": str(round(15 + (i % 70) * 1.1, 2)),
            "promo": bool(i % 37 == 0), "stockout": bool(i % 109 == 0),
        }


def generate_invalid_rows():
    base = {"date": date.today().isoformat(), "store_id": "S001", "sku": "SKU00001",
            "transaction_id": "INVALID-1", "line_id": "1", "qty": 5,
            "unit_price": "10.00", "unit_cost": "5.00"}
    return [
        {**base, "qty": -5},
        {**base, "transaction_id": "INVALID-2", "sku": "DOES_NOT_EXIST"},
        {**base, "transaction_id": "INVALID-3", "sku": ""},
        {**base, "transaction_id": "INVALID-4", "qty": 3},
        {**base, "transaction_id": "INVALID-4", "qty": 3},
    ]


def write_fixtures(folder: str, seed: int):
    os.makedirs(folder, exist_ok=True)
    columns = ["date", "store_id", "sku", "transaction_id", "line_id",
               "qty", "unit_price", "unit_cost", "promo", "stockout"]
    for filename, rows in (("valid_sales.csv", generate_rows(150, seed)),
                           ("invalid_sales.csv", generate_invalid_rows())):
        with open(os.path.join(folder, filename), "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)


def seed(rows: int, seed: int):
    create_catalog(seed)
    if rows < 120:
        raise ValueError("Demo needs at least 120 rows")
    batch, offset = [], 0
    for item in generate_rows(rows, seed):
        batch.append(item)
        if len(batch) == 2500:
            result = ingest("demo_sales", f"seed-{seed}-{offset}", batch)
            print(f"{offset + len(batch)}/{rows}: {result.status} dataset v{result.dataset_version}")
            offset += len(batch)
            batch = []
    if batch:
        result = ingest("demo_sales", f"seed-{seed}-{offset}", batch)
        print(f"{offset + len(batch)}/{rows}: {result.status} dataset v{result.dataset_version}")
    write_fixtures("demo", seed)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rows", type=int, default=2500)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    seed(args.rows, args.seed)
