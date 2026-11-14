"""Measure actual throughput and query p50/p95; never fabricate figures."""
import argparse
import json
import platform
import statistics
import time
from uuid import uuid4
from sqlalchemy import func, select
from backend.app.db import SessionLocal
from backend.app.models import DailySalesMart
from backend.app.pipeline import ingest
from backend.scripts.seed_demo import create_catalog, generate_rows


def benchmark(rows: int, seed: int):
    create_catalog(seed)
    data = list(generate_rows(rows, seed))
    started = time.perf_counter()
    statuses = []
    for offset in range(0, len(data), 100000):
        run = ingest("demo_sales", f"benchmark-{uuid4()}", data[offset:offset + 100000])
        statuses.append(run.status)
    seconds = time.perf_counter() - started
    times = []
    with SessionLocal() as db:
        for _ in range(50):
            before = time.perf_counter()
            db.execute(select(func.sum(DailySalesMart.revenue)).where(
                DailySalesMart.store_id == "S001")).scalar_one()
            times.append((time.perf_counter() - before) * 1000)
    times.sort()
    print(json.dumps({
        "host": platform.platform(), "python": platform.python_version(),
        "rows": rows, "seed": seed, "ingestSeconds": round(seconds, 3),
        "statuses": statuses, "queryMsP50": round(statistics.median(times), 3),
        "queryMsP95": round(times[47], 3),
        "note": "Document CPU, RAM, storage, DB and concurrency before reporting results",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    benchmark(args.rows, args.seed)
