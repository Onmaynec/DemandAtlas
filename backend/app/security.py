"""Shared PostgreSQL/SQLite rate limits and minimal persistent audit events.

No plaintext Bearer key, source row, request body or response payload is stored.
"""
import hashlib
import logging
import time
from datetime import timedelta

from fastapi import Request
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .auth import utc_now
from .db import SessionLocal
from .models import AccessAudit, RateLimitWindow

logger = logging.getLogger(__name__)


def _touch_window(db, raw_identity: str, minute: int) -> int:
    digest = hashlib.sha256(f"{minute}:{raw_identity}".encode()).hexdigest()
    values = {"key": digest, "hits": 1}
    if db.bind.dialect.name == "postgresql":
        statement = pg_insert(RateLimitWindow).values(values)
    elif db.bind.dialect.name == "sqlite":
        statement = sqlite_insert(RateLimitWindow).values(values)
    else:
        raise RuntimeError("Rate limit database dialect not supported")
    statement = statement.on_conflict_do_update(
        index_elements=["key"], set_={"hits": RateLimitWindow.hits + 1}
    ).returning(RateLimitWindow.hits)
    return int(db.scalar(statement))


def limit_request(request: Request, limit: int) -> bool:
    """Per-IP and per-credential atomic minute buckets; works across API replicas."""
    minute = int(time.time() // 60)
    client = request.client.host if request.client else "unknown"
    credential = request.headers.get("authorization", "")
    # Both limits apply. Untrusted proxy headers are deliberately ignored.
    ip_hash = hashlib.sha256(client.encode()).hexdigest()
    with SessionLocal.begin() as db:
        ip_count = _touch_window(db, "ip:" + ip_hash, minute)
        token_count = 0
        if credential:
            token_hash = hashlib.sha256(credential.encode()).hexdigest()
            token_count = _touch_window(db, "token:" + token_hash, minute)
    return ip_count > limit or token_count > limit


def audit_mutation(request: Request, status_code: int):
    """Record every successful/denied mutation, not its sensitive payload."""
    actor = getattr(request.state, "principal", None)
    record = AccessAudit(
        request_id=request.state.request_id,
        actor_id=actor.identity_id if actor else None,
        actor_name=actor.name if actor else "anonymous",
        actor_role=actor.role if actor else "anonymous",
        method=request.method,
        path=request.url.path[:250],
        status_code=status_code,
    )
    with SessionLocal.begin() as db:
        db.add(record)


def clean_rate_limits():
    """Called by the scheduler; otherwise bucket rows are permanent."""
    cutoff = utc_now() - timedelta(hours=2)
    with SessionLocal.begin() as db:
        return db.execute(delete(RateLimitWindow).where(
            RateLimitWindow.created_at < cutoff
        )).rowcount
