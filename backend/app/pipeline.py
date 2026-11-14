"""CSV/SQL ingestion and atomic mart publication.

Raw and findings commit before staging. A failed quality gate never changes
staging, inventory, dataset_version, or published marts.
"""
import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import Integer, func, insert, select, text, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .db import SessionLocal
from .models import (
    DailyCoverage, DailySalesMart, DatasetState, Inventory, PipelineRun,
    Product, QualityFinding, RawSale, RawStock, Source, StagedSale, Store
)

MAX_ROWS = 100000
MAX_CSV_BYTES = 35 * 1024 * 1024


def parse_csv(csv_text: str) -> list[dict]:
    if len(csv_text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError("CSV exceeds 35 MB; split it into batches")
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        raise ValueError("CSV header missing")
    rows = []
    for row in reader:
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError("Batch exceeds 100,000 records")
    return rows


def fetch_sql(source: Source) -> list[dict]:
    # Never execute a caller-provided SQL string. Only a single safe table name.
    table = (source.config or {}).get("table", "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", table):
        raise ValueError("SQL connector needs a simple allowlisted table name")
    from .db import engine
    with engine.connect() as connection:
        result = connection.execute(text(f"SELECT * FROM {table} LIMIT :limit"), {"limit": MAX_ROWS + 1})
        rows = [json.loads(json.dumps(dict(row._mapping), default=str)) for row in result]
    if len(rows) > MAX_ROWS:
        raise ValueError("SQL source exceeds 100,000 rows per batch")
    return rows


def bulk_ignore(session, model, rows):
    if not rows:
        return
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(model).values(rows).on_conflict_do_nothing()
    elif dialect == "sqlite":
        stmt = sqlite_insert(model).values(rows).on_conflict_do_nothing()
    else:
        raise RuntimeError("Only PostgreSQL and SQLite test dialects are supported")
    session.execute(stmt)


def validate(rows: list[dict], entity: str, stores: set, products: set) -> tuple[list[dict], list[dict]]:
    findings, accepted = [], []
    seen = set()
    store_dates = defaultdict(set)
    for number, original in enumerate(rows, start=2):  # CSV header = row 1
        row = dict(original)
        errors = []

        def required(key):
            val = row.get(key)
            if val is None or str(val).strip() == "":
                errors.append((f"null_{key}", f"Required key {key} missing"))
                return ""
            return str(val).strip()

        store = required("store_id")
        sku = required("sku")
        if store and store not in stores:
            errors.append(("unknown_store", f"Unknown store: {store}"))
        if sku and sku not in products:
            errors.append(("unknown_sku", f"Unknown SKU: {sku}"))
        if entity == "sales":
            transaction = required("transaction_id")
            line = required("line_id")
            date_str = required("date")
            try:
                day = date.fromisoformat(date_str)
            except ValueError:
                errors.append(("invalid_date", "Date must be YYYY-MM-DD"))
                day = None
            try:
                qty = int(str(row.get("qty", "")))
                if qty < 0:
                    errors.append(("negative_qty", "Quantity cannot be negative"))
            except ValueError:
                qty = 0
                errors.append(("invalid_qty", "Quantity must be integer"))
            try:
                price = Decimal(str(row.get("unit_price", "")))
                cost = Decimal(str(row.get("unit_cost", "0")))
                if not price.is_finite() or not cost.is_finite() or price < 0 or cost < 0:
                    errors.append(("invalid_price", "Prices must be finite and non-negative"))
                if price.is_finite() and cost.is_finite() and (price.as_tuple().exponent < -2 or cost.as_tuple().exponent < -2):
                    errors.append(("invalid_price_precision", "Prices have at most two decimal places"))
            except InvalidOperation:
                price = cost = Decimal(0)
                errors.append(("invalid_price", "Price must be numeric"))
            key = (transaction, line)
            if transaction and line and key in seen:
                errors.append(("duplicate", "Duplicate transactionId/lineId in batch"))
            seen.add(key)
            if errors:
                findings.extend({"row_number": number, "rule": code, "severity": "critical",
                                 "message": message} for code, message in errors)
                continue
            store_dates[store].add(day)
            accepted.append({
                "transaction_id": transaction, "line_id": line, "date": day,
                "store_id": store, "sku": sku, "qty": qty,
                "unit_price": price, "unit_cost": cost,
                "promo": str(row.get("promo", "")).lower() in ("1", "true", "yes"),
                "stockout": str(row.get("stockout", "")).lower() in ("1", "true", "yes"),
            })
        else:
            for column in ("on_hand", "inbound"):
                try:
                    row[column] = int(str(row.get(column, "0")))
                    if row[column] < 0:
                        errors.append(("negative_" + column, f"{column} cannot be negative"))
                except ValueError:
                    errors.append(("invalid_" + column, f"{column} must be integer"))
            key = (store, sku)
            if store and sku and key in seen:
                errors.append(("duplicate", "Duplicate store/SKU stock row"))
            seen.add(key)
            if errors:
                findings.extend({"row_number": number, "rule": code, "severity": "critical",
                                 "message": message} for code, message in errors)
                continue
            accepted.append({"store_id": store, "sku": sku,
                             "on_hand": row["on_hand"], "inbound": row["inbound"]})
    for store, days in store_dates.items():
        for index in range((max(days) - min(days)).days + 1):
            day = min(days) + timedelta(days=index)
            if day not in days:
                findings.append({"row_number": 0, "rule": "missing_store_day",
                                 "severity": "warning", "message": f"{store}: no source rows on {day}"})
    return accepted, findings


def ingest(source_id: str, key: str, rows: list[dict]) -> PipelineRun:
    if not key or len(key) > 128:
        raise ValueError("idempotencyKey is required (max 128 chars)")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError("Batch must contain at most 100,000 rows")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Every row must be an object")
    checksum = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
    try:
        with SessionLocal.begin() as session:
            source = session.get(Source, source_id)
            if not source or not source.enabled:
                raise LookupError("Source does not exist or is disabled")
            existing = session.scalar(select(PipelineRun).where(
                PipelineRun.source_id == source_id, PipelineRun.idempotency_key == key
            ))
            if existing:
                if existing.checksum != checksum or existing.mode != "append":
                    raise RuntimeError("Idempotency key reused with different content")
                return existing
            store_ids = set(session.scalars(select(Store.id)))
            sku_ids = set(session.scalars(select(Product.sku)))
            entity = source.entity
            accepted, findings = validate(rows, entity, store_ids, sku_ids)
            run = PipelineRun(
                id=str(uuid4()), source_id=source_id, idempotency_key=key,
                checksum=checksum, schema_version=source.schema_version, mode="append",
                status="rejected" if any(f["severity"] == "critical" for f in findings) else "validated",
                rows_received=len(rows), rows_accepted=len(accepted),
                rows_rejected=len(rows) - len(accepted), finished_at=None,
            )
            session.add(run)
            session.flush()
            raw_model = RawSale if entity == "sales" else RawStock
            for pos in range(0, len(rows), 1500):
                session.execute(insert(raw_model), [
                    {"run_id": run.id, "row_number": n + 2, "payload": rows[n]}
                    for n in range(pos, min(pos + 1500, len(rows)))
                ])
            for pos in range(0, len(findings), 1500):
                session.execute(insert(QualityFinding), [
                    {"run_id": run.id, **f} for f in findings[pos:pos + 1500]
                ])
            if run.status == "rejected":
                run.finished_at = datetime.now(timezone.utc)
                return run
            run_id = run.id
    except IntegrityError:
        # PostgreSQL unique(source, idempotency_key) decides the race.
        # A concurrent winning request may have committed its raw phase
        # while its atomic mart publication is still in progress.
        with SessionLocal() as retry:
            winner = retry.scalar(select(PipelineRun).where(
                PipelineRun.source_id == source_id,
                PipelineRun.idempotency_key == key,
            ))
            if winner is None:
                raise
            if winner.checksum != checksum or winner.mode != "append":
                raise RuntimeError("Idempotency key reused with different content")
            return winner
    # Everything changing the published dataset is in ONE transaction.
    try:
        with SessionLocal.begin() as session:
            run = session.get(PipelineRun, run_id)
            state = session.get(DatasetState, 1, with_for_update=True)
            if state is None:
                state = DatasetState(id=1, version=0)
                session.add(state)
                session.flush()
            if entity == "sales":
                # Compare facts after acquiring the dataset lock. A previously
                # published transaction may be replayed with identical values;
                # a changed payload needs explicit version-checked reprocessing.
                keys = [(r["transaction_id"], r["line_id"]) for r in accepted]
                current = {}
                for off in range(0, len(keys), 300):
                    existing_rows = session.scalars(select(StagedSale).where(
                        StagedSale.source_id == source_id,
                        tuple_(StagedSale.transaction_id, StagedSale.line_id).in_(keys[off:off + 300]),
                    )).all()
                    current.update({(r.transaction_id, r.line_id): r for r in existing_rows})
                rejected_rows = []
                fresh = []
                for row in accepted:
                    key_pair = (row["transaction_id"], row["line_id"])
                    prior = current.get(key_pair)
                    if prior is None:
                        fresh.append(row)
                    elif any([
                        prior.date != row["date"], prior.store_id != row["store_id"],
                        prior.sku != row["sku"], prior.qty != row["qty"],
                        prior.unit_price != row["unit_price"],
                        prior.unit_cost != row["unit_cost"],
                        bool(prior.promo) != row["promo"],
                        bool(prior.stockout) != row["stockout"],
                    ]):
                        rejected_rows.append(key_pair)
                if rejected_rows:
                    line_numbers = {
                        (str(r.get("transaction_id", "")).strip(),
                         str(r.get("line_id", "")).strip()): n
                        for n, r in enumerate(rows, start=2)
                    }
                    session.execute(insert(QualityFinding), [
                        {"run_id": run_id, "row_number": line_numbers[tx],
                         "rule": "changed_published_fact", "severity": "critical",
                         "message": "Conflicting published sale; use version-checked reprocessing"}
                        for tx in rejected_rows
                    ])
                    run.status = "rejected"
                    run.rows_rejected = len(rejected_rows)
                    run.rows_accepted = len(rows) - len(rejected_rows)
                    run.finished_at = datetime.now(timezone.utc)
                    return run
                if not fresh:
                    run.status = "published"
                    run.dataset_version = state.version
                    run.finished_at = datetime.now(timezone.utc)
                    return run
            state.version += 1
            if entity == "sales":
                touched = {(r["date"], r["store_id"], r["sku"]) for r in fresh}
                for pos in range(0, len(fresh), 500):
                    bulk_ignore(session, StagedSale, [
                        {"source_id": source_id, "run_id": run_id, **record}
                        for record in fresh[pos:pos + 500]
                    ])
                touched_list = sorted(touched)
                for pos in range(0, len(touched_list), 100):
                    subset = touched_list[pos:pos + 100]
                    aggregates = session.execute(select(
                        StagedSale.date, StagedSale.store_id, StagedSale.sku,
                        func.sum(StagedSale.qty),
                        func.sum(StagedSale.qty * StagedSale.unit_price),
                        func.sum(StagedSale.qty * StagedSale.unit_cost),
                        func.max(StagedSale.promo.cast(Integer)),
                        func.max(StagedSale.stockout.cast(Integer)),
                    ).where(
                        tuple_(StagedSale.date, StagedSale.store_id, StagedSale.sku).in_(subset)
                    ).group_by(StagedSale.date, StagedSale.store_id, StagedSale.sku)).all()
                    records = [
                        {"date": a[0], "store_id": a[1], "sku": a[2],
                         "units": int(a[3]), "revenue": a[4], "cost": a[5],
                         "promo": bool(a[6]), "stockout": bool(a[7]),
                         "dataset_version": state.version} for a in aggregates
                    ]
                    if records:
                        if session.bind.dialect.name == "postgresql":
                            statement = pg_insert(DailySalesMart).values(records)
                        else:
                            statement = sqlite_insert(DailySalesMart).values(records)
                        updates = {col: getattr(statement.excluded, col) for col in (
                            "units", "revenue", "cost", "promo", "stockout", "dataset_version"
                        )}
                        session.execute(statement.on_conflict_do_update(
                            index_elements=["date", "store_id", "sku"], set_=updates
                        ))
                coverage = sorted({(r["date"], r["store_id"]) for r in fresh})
                for pos in range(0, len(coverage), 500):
                    bulk_ignore(session, DailyCoverage, [
                        {"date": day, "store_id": store, "run_id": run_id}
                        for day, store in coverage[pos:pos + 500]
                    ])
            else:
                for item in accepted:
                    inv = session.scalar(select(Inventory).where(
                        Inventory.store_id == item["store_id"], Inventory.sku == item["sku"]
                    ))
                    if not inv:
                        inv = Inventory(store_id=item["store_id"], sku=item["sku"])
                        session.add(inv)
                    inv.on_hand = item["on_hand"]
                    inv.inbound = item["inbound"]
                    inv.updated_at = datetime.now(timezone.utc)
            run.status = "published"
            run.dataset_version = state.version
            run.finished_at = datetime.now(timezone.utc)
    except Exception as exc:
        with SessionLocal.begin() as session:
            run = session.get(PipelineRun, run_id)
            run.status = "failed"
            run.error = str(exc)[:1000]
            run.finished_at = datetime.now(timezone.utc)
        raise
    with SessionLocal() as session:
        return session.get(PipelineRun, run_id)
