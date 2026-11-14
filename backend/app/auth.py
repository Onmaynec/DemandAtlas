"""Database-backed Bearer identity and role-scoped access.

Only short-lived, random, high-entropy tokens are issued; SHA-256 fingerprints
are persisted. Demo environment credentials are DISABLED outside DEMO_MODE.
"""
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from .db import SessionLocal
from .models import AccessKey, AccessUser

BUSINESS_ROLES = (
    "data_engineer", "analyst", "category_manager", "director", "observer"
)
ACCESS_ROLES = BUSINESS_ROLES + ("access_admin",)


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    stores: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    identity_id: str | None = None
    key_id: str | None = None


def utc_now():
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_demo_mode() -> bool:
    if os.getenv("APP_ENV", "local").lower() == "production":
        return False
    return os.getenv("DEMO_MODE", "0") == "1"


def validate_runtime():
    if os.getenv("APP_ENV", "local").lower() == "production":
        if os.getenv("DEMO_MODE", "0") == "1":
            raise RuntimeError("DEMO_MODE=1 is not permitted in production")
        for key in ("TOKEN_ENGINEER", "TOKEN_ANALYST", "TOKEN_CATEGORY",
                    "TOKEN_DIRECTOR", "TOKEN_OBSERVER"):
            if os.getenv(key):
                raise RuntimeError(f"Static demonstration token {key} is not allowed in production")
        service_key = os.getenv("FORECAST_SERVICE_TOKEN", "")
        if not service_key or service_key == "change-me-before-deployment":
            raise RuntimeError("A non-default FORECAST_SERVICE_TOKEN is required in production")


def principals():
    if not is_demo_mode():
        return {}
    profiles = [
        ("engineer", "data_engineer", (), ()),
        ("analyst", "analyst", (), ()),
        ("category", "category_manager", (), ("Food",)),
        ("director", "director", (), ()),
        ("observer", "observer", ("S001", "S002"), ()),
    ]
    output = {}
    for name, role, stores, categories in profiles:
        token = os.getenv("TOKEN_" + name.upper()) or "demo-" + name
        output[token] = Principal(name, role, stores, categories)
    return output


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_key(db, user: AccessUser, label: str, actor: str,
              expires_in_days: int = 90):
    if not 1 <= expires_in_days <= 365:
        raise ValueError("Token lifetime must be from 1 to 365 days")
    if not user.enabled:
        raise ValueError("Cannot issue credentials to a disabled user")
    token = "da_" + secrets.token_urlsafe(32)
    access_key = AccessKey(
        id=str(uuid4()), user_id=user.id, token_hash=fingerprint(token),
        prefix=token[:10], label=label, created_by=actor,
        expires_at=utc_now() + timedelta(days=expires_in_days),
    )
    db.add(access_key)
    return token, access_key


def authenticate(authorization: str | None) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    incoming = authorization[7:]
    if len(incoming) > 512 or len(incoming) < 3:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    # Existing demo clients keep working ONLY in explicitly enabled local mode.
    if is_demo_mode():
        for known, principal in principals().items():
            if secrets.compare_digest(known, incoming):
                return principal
    if not incoming.startswith("da_"):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    with SessionLocal() as db:
        result = db.execute(select(AccessKey, AccessUser).join(
            AccessUser, AccessUser.id == AccessKey.user_id
        ).where(AccessKey.token_hash == fingerprint(incoming))).one_or_none()
        if not result:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        key, user = result
        if (key.revoked_at is not None or
                as_utc(key.expires_at) <= utc_now() or not user.enabled):
            raise HTTPException(status_code=401, detail="Credentials expired or revoked")
        if user.role not in ACCESS_ROLES:
            raise HTTPException(status_code=403, detail="Unknown role")
        return Principal(
            name=user.name, role=user.role,
            stores=tuple(user.stores or ()),
            categories=tuple(user.categories or ()),
            identity_id=user.id, key_id=key.id,
        )


def current_user(request: Request,
                 authorization: str | None = Header(default=None)) -> Principal:
    user = authenticate(authorization)
    request.state.principal = user
    return user


def roles(*allowed: str):
    def check(user: Principal = Depends(current_user)):
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Insufficient role")
        return user
    return check


def business_user(user: Principal = Depends(current_user)):
    if user.role not in ("analyst", "category_manager", "director", "observer"):
        raise HTTPException(status_code=403, detail="Business reporting not permitted")
    return user


def can_access(user: Principal, store: str | None = None,
               category: str | None = None) -> bool:
    return ((not user.stores or store in user.stores or store is None)
            and (not user.categories or category in user.categories or category is None))


def require_scope(user: Principal, store: str | None = None,
                  category: str | None = None):
    if not can_access(user, store, category):
        raise HTTPException(status_code=403, detail="Entity is outside permitted scope")
