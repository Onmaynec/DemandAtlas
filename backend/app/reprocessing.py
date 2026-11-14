"""Explicit, version-checked replacement of one source/store/date partition.

Reprocessing is a snapshot operation, not a patch to individual transactions:
the submitted rows are the complete authoritative slice for the selected
source, store and date range. An empty list means verified zero sales.
All changes to staging, marts and dataset version share one transaction.
"""
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Integer, delete, func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .auth import Principal, roles
from .db import SessionLocal
from .models import (
    DailyCoverage, DailySalesMart, DatasetState, PipelineRun,
    Product, QualityFinding, RawSale, Source, StagedSale, Store,
)
from .pipeline import MAX_ROWS, validate

router = APIRouter(prefix="/api/v1")


class ReplaceInput(BaseModel):
    idempotencyKey: str = Field(min_length=1, max_length=128)
    expectedDatasetVersion: int = Field(ge=0)
    start: date
    end: date
    storeId: str = Field(min_length=1, max_length=32)
    reason: str = Field(min_length=12, max_length=400)
    rows: list[dict] = Field(max_length=MAX_ROWS)


def checksum_for(body: ReplaceInput) -> str:
    # Excludes expectedDatasetVersion: exact retries remain valid after the first
    # successful commit; the original precondition is checked only for new keys.
    content = {
        "mode": "replace", "start": body.start.isoformat(),
        "end": body.end.isoformat(), "store": body.storeId,
        "reason": body.reason, "rows": body.rows,
    }
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def replace_range(source_id: str, body: ReplaceInput) -> dict:
    if body.end < body.start or (body.end - body.start).days > 30:
        raise ValueError("A replacement snapshot covers 1–31 consecutive days")
    if any(not isinstance(row, dict) for row in body.rows):
        raise ValueError("Every source row must be an object")
    checksum = checksum_for(body)
    with SessionLocal.begin() as db:
        # Serialize all mart writers on this singleton row; existing ingestion
        # also locks this row before attempting to publish.
        state = db.get(DatasetState, 1, with_for_update=True)
        if state is None:
            state = DatasetState(id=1, version=0)
            db.add(state)
            db.flush()
        previous = db.scalar(select(PipelineRun).where(
            PipelineRun.source_id == source_id,
            PipelineRun.idempotency_key == body.idempotencyKey,
        ))
        if previous:
            if previous.mode != "replace" or previous.checksum != checksum:
                raise RuntimeError("Idempotency key reused for a different operation or payload")
            return serialize(previous)
        if body.expectedDatasetVersion != state.version:
            raise RuntimeError(
                f"Stale dataset version: expected {body.expectedDatasetVersion}, current {state.version}"
            )
        source = db.get(Source, source_id)
        if source is None or not source.enabled or source.entity != "sales":
            raise LookupError("Enabled sales source not found")
        if not db.get(Store, body.storeId):
            raise LookupError("Store not found")
        stores = set(db.scalars(select(Store.id)))
        products = set(db.scalars(select(Product.sku)))
        accepted, findings = validate(body.rows, "sales", stores, products)
        row_numbers = {}
        for idx, original in enumerate(body.rows, start=2):
            tx, line = str(original.get("transaction_id", "")).strip(), str(original.get("line_id", "")).strip()
            row_numbers[(tx, line)] = idx
        for row in accepted:
            if row["store_id"] != body.storeId or not body.start <= row["date"] <= body.end:
                findings.append({
                    "row_number": row_numbers[(row["transaction_id"], row["line_id"])],
                    "rule": "outside_replacement_scope",
                    "severity": "critical",
                    "message": "Row store/date must belong to the declared replacement partition",
                })
        # The same transaction identity cannot be silently moved from another
        # store or outside the time window by a partition replacement.
        for offset in range(0, len(accepted), 300):
            slice_ = accepted[offset:offset + 300]
            keys = [(r["transaction_id"], r["line_id"]) for r in slice_]
            existing = db.execute(select(
                StagedSale.transaction_id, StagedSale.line_id, StagedSale.store_id, StagedSale.date
            ).where(StagedSale.source_id == source_id,
                    tuple_(StagedSale.transaction_id, StagedSale.line_id).in_(keys))).all()
            for tx, line, store, day in existing:
                if store != body.storeId or not body.start <= day <= body.end:
                    findings.append({
                        "row_number": row_numbers[(tx, line)],
                        "rule": "transaction_outside_scope", "severity": "critical",
                        "message": "Transaction identity already belongs to another partition",
                    })
        failed_lines = {f["row_number"] for f in findings if f["severity"] == "critical"}
        run = PipelineRun(
            id=str(uuid4()), source_id=source_id,
            idempotency_key=body.idempotencyKey, checksum=checksum,
            schema_version=source.schema_version, mode="replace",
            scope={"start": body.start.isoformat(), "end": body.end.isoformat(),
                   "storeId": body.storeId, "reason": body.reason},
            status="rejected" if failed_lines else "validated",
            rows_received=len(body.rows), rows_accepted=len(body.rows)-len(failed_lines),
            rows_rejected=len(failed_lines),
        )
        db.add(run)
        db.flush()
        for offset in range(0, len(body.rows), 1000):
            db.execute(insert(RawSale), [
                {"run_id": run.id, "row_number": n + 2, "payload": body.rows[n]}
                for n in range(offset, min(len(body.rows), offset + 1000))
            ])
        for offset in range(0, len(findings), 1000):
            db.execute(insert(QualityFinding), [
                {"run_id": run.id, **f} for f in findings[offset:offset + 1000]
            ])
        if failed_lines:
            run.finished_at = datetime.now(timezone.utc)
            return serialize(run)
        # Revisions never update old raw or old published forecasts.
        old_partitions = db.execute(select(
            StagedSale.date, StagedSale.store_id, StagedSale.sku
        ).where(StagedSale.source_id == source_id,
                StagedSale.store_id == body.storeId,
                StagedSale.date.between(body.start, body.end))).all()
        touched = set(old_partitions)
        touched.update((r["date"], r["store_id"], r["sku"]) for r in accepted)
        db.execute(delete(StagedSale).where(
            StagedSale.source_id == source_id, StagedSale.store_id == body.storeId,
            StagedSale.date.between(body.start, body.end)
        ))
        for offset in range(0, len(accepted), 500):
            db.execute(insert(StagedSale), [
                {"source_id": source_id, "run_id": run.id, **row}
                for row in accepted[offset:offset + 500]
            ])
        state.version += 1
        # Recompute each affected partition from ALL contributing sources.
        for offset in range(0, len(touched), 100):
            partitions = sorted(touched)[offset:offset + 100]
            groups = db.execute(select(
                StagedSale.date, StagedSale.store_id, StagedSale.sku,
                func.sum(StagedSale.qty),
                func.sum(StagedSale.qty * StagedSale.unit_price),
                func.sum(StagedSale.qty * StagedSale.unit_cost),
                func.max(StagedSale.promo.cast(Integer)),
                func.max(StagedSale.stockout.cast(Integer)),
            ).where(
                tuple_(StagedSale.date, StagedSale.store_id, StagedSale.sku).in_(partitions)
            ).group_by(StagedSale.date, StagedSale.store_id, StagedSale.sku)).all()
            present = {(g[0], g[1], g[2]) for g in groups}
            missing = set(partitions) - present
            if missing:
                db.execute(delete(DailySalesMart).where(
                    tuple_(DailySalesMart.date, DailySalesMart.store_id,
                           DailySalesMart.sku).in_(list(missing))
                ))
            records = [
                {"date": g[0], "store_id": g[1], "sku": g[2],
                 "units": int(g[3]), "revenue": g[4], "cost": g[5],
                 "promo": bool(g[6]), "stockout": bool(g[7]),
                 "dataset_version": state.version}
                for g in groups
            ]
            if records:
                dialect = db.bind.dialect.name
                if dialect == "postgresql":
                    statement = pg_insert(DailySalesMart).values(records)
                elif dialect == "sqlite":
                    statement = sqlite_insert(DailySalesMart).values(records)
                else:
                    raise RuntimeError("Unsupported database for snapshot replacement")
                db.execute(statement.on_conflict_do_update(
                    index_elements=["date", "store_id", "sku"],
                    set_={name: getattr(statement.excluded, name) for name in (
                        "units", "revenue", "cost", "promo", "stockout", "dataset_version"
                    )}
                ))
        # A complete source snapshot can prove a zero-sales day. It is not
        # inferred from absence of receipts in the usual append batch.
        first = body.start
        days = []
        while first <= body.end:
            days.append(first)
            first += timedelta(days=1)
        present_coverage = set(db.scalars(select(DailyCoverage.date).where(
            DailyCoverage.store_id == body.storeId,
            DailyCoverage.date.between(body.start, body.end),
        )))
        for day in days:
            if day not in present_coverage:
                db.add(DailyCoverage(date=day, store_id=body.storeId, run_id=run.id))
        run.status = "published"
        run.dataset_version = state.version
        run.finished_at = datetime.now(timezone.utc)
        return serialize(run)


def serialize(run: PipelineRun) -> dict:
    return {"runId": run.id, "status": run.status,
            "datasetVersion": run.dataset_version,
            "rowsReceived": run.rows_received, "rowsRejected": run.rows_rejected,
            "mode": run.mode, "scope": run.scope}


@router.post("/pipelines/{source_id}/reprocess")
def reprocess(source_id: str, body: ReplaceInput,
              user: Principal = Depends(roles("data_engineer"))):
    try:
        return replace_range(source_id, body)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
