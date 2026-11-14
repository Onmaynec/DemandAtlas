"""Scheduled SQL ingestion; database leases avoid duplicate claims."""
import logging
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from .db import SessionLocal
from .models import Source
from .pipeline import fetch_sql, ingest

logging.basicConfig(level="INFO")


def run_due():
    with SessionLocal.begin() as session:
        due = session.scalars(select(Source).where(
            Source.enabled.is_(True), Source.schedule_minutes.is_not(None),
            Source.next_run_at <= datetime.now(timezone.utc), Source.kind == "sql",
        ).with_for_update(skip_locked=True).limit(10)).all()
        jobs = []
        for source in due:
            stamp = source.next_run_at
            source.next_run_at = datetime.now(timezone.utc) + timedelta(minutes=source.schedule_minutes)
            jobs.append((source.id, stamp.isoformat()))
    for source_id, stamp in jobs:
        try:
            with SessionLocal() as db:
                source = db.get(Source, source_id)
                rows = fetch_sql(source)
            result = ingest(source_id, "scheduled-" + stamp, rows)
            logging.info("source=%s run=%s status=%s", source_id, result.id, result.status)
        except Exception:
            logging.exception("Scheduled ingestion failed: %s", source_id)


if __name__ == "__main__":
    while True:
        try:
            run_due()
        except Exception:
            logging.exception("Scheduler tick failed")
        time.sleep(30)
