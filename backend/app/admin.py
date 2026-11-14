"""Administrative API for local identities and renewable access keys.

Only the access_admin role is authorized. Plaintext secrets are emitted
exactly once at creation/rotation; no list/read route ever exposes them.
"""
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import ACCESS_ROLES, Principal, issue_key, roles, utc_now
from .db import get_db
from .models import AccessAudit, AccessKey, AccessUser, Product, Store

router = APIRouter(prefix="/api/v1/admin", tags=["Access management"])


class NewUser(BaseModel):
    name: str = Field(min_length=2, max_length=70, pattern=r"^[a-zA-Z0-9_.-]+$")
    role: str
    stores: list[str] = Field(default_factory=list, max_length=100)
    categories: list[str] = Field(default_factory=list, max_length=100)
    expiresInDays: int = Field(default=90, ge=1, le=365)


class ChangeUser(BaseModel):
    role: str | None = None
    stores: list[str] | None = Field(default=None, max_length=100)
    categories: list[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None


class NewKey(BaseModel):
    label: str = Field(min_length=1, max_length=70)
    expiresInDays: int = Field(default=90, ge=1, le=365)


def validate_scope(db: Session, role: str, stores: list[str],
                   categories: list[str]):
    if role not in ACCESS_ROLES:
        raise HTTPException(400, "Unknown role")
    if len(stores) != len(set(stores)) or len(categories) != len(set(categories)):
        raise HTTPException(400, "Duplicate store/category assignment")
    if role == "category_manager" and not categories:
        raise HTTPException(400, "Category manager requires assigned categories")
    if role == "observer" and not stores:
        raise HTTPException(400, "Observer requires assigned stores")
    if role == "access_admin" and (stores or categories):
        raise HTTPException(400, "Access administrator cannot have business scopes")
    if stores:
        existing = set(db.scalars(select(Store.id).where(Store.id.in_(stores))))
        if existing != set(stores):
            raise HTTPException(400, "Assignment references an unknown store")
    if categories:
        known = set(db.scalars(select(Product.category).distinct()))
        if set(categories) - known:
            raise HTTPException(400, "Assignment references an unknown category")


def public_user(user: AccessUser):
    return {
        "id": user.id, "name": user.name, "role": user.role,
        "stores": user.stores, "categories": user.categories,
        "enabled": user.enabled, "createdAt": user.created_at,
        "updatedAt": user.updated_at,
    }


def public_key(key: AccessKey):
    return {
        "id": key.id, "userId": key.user_id, "prefix": key.prefix,
        "label": key.label, "createdBy": key.created_by,
        "createdAt": key.created_at, "expiresAt": key.expires_at,
        "revokedAt": key.revoked_at,
    }


def transaction_audit(db: Session, request: Request, actor: Principal,
                      status_code: int):
    db.add(AccessAudit(
        request_id=request.state.request_id, actor_id=actor.identity_id,
        actor_name=actor.name, actor_role=actor.role,
        method=request.method, path=request.url.path[:250],
        status_code=status_code,
    ))


@router.get("/users")
def users(actor: Principal = Depends(roles("access_admin")),
          db: Session = Depends(get_db)):
    values = list(db.scalars(select(AccessUser).order_by(AccessUser.name)))
    return [public_user(u) for u in values]


@router.post("/users", status_code=201)
def create_user(body: NewUser, request: Request,
                actor: Principal = Depends(roles("access_admin")),
                db: Session = Depends(get_db)):
    validate_scope(db, body.role, body.stores, body.categories)
    if db.scalar(select(AccessUser.id).where(AccessUser.name == body.name)):
        raise HTTPException(409, "Identity name already exists")
    user = AccessUser(
        id=str(uuid4()), name=body.name, role=body.role,
        stores=body.stores, categories=body.categories, enabled=True,
    )
    db.add(user)
    # Materialize the parent before issuing an AccessKey. SQLite may allow
    # unflushed FK ordering silently, but PostgreSQL enforces it.
    db.flush()
    token, key = issue_key(db, user, "initial", actor.name, body.expiresInDays)
    transaction_audit(db, request, actor, 201)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Identity name already exists") from exc
    return {**public_user(user), "key": public_key(key),
            "tokenOnce": token}


@router.patch("/users/{user_id}")
def change_user(user_id: str, body: ChangeUser, request: Request,
                actor: Principal = Depends(roles("access_admin")),
                db: Session = Depends(get_db)):
    user = db.get(AccessUser, user_id, with_for_update=True)
    if not user:
        raise HTTPException(404, "Identity not found")
    new_role = body.role if body.role is not None else user.role
    new_stores = body.stores if body.stores is not None else list(user.stores)
    new_categories = (body.categories if body.categories is not None
                      else list(user.categories))
    new_enabled = body.enabled if body.enabled is not None else user.enabled
    validate_scope(db, new_role, new_stores, new_categories)
    if actor.identity_id == user.id and (
        new_role != "access_admin" or not new_enabled
    ):
        raise HTTPException(409, "Cannot revoke your own administrative identity")
    if user.role == "access_admin" and (
        new_role != "access_admin" or not new_enabled
    ):
        admins = db.scalar(select(func.count()).select_from(AccessUser).where(
            AccessUser.role == "access_admin", AccessUser.enabled.is_(True)
        )) or 0
        if admins <= 1:
            raise HTTPException(409, "Cannot disable the last administrator")
    user.role = new_role
    user.stores = new_stores
    user.categories = new_categories
    user.enabled = new_enabled
    user.updated_at = utc_now()
    transaction_audit(db, request, actor, 200)
    db.commit()
    return public_user(user)


@router.get("/users/{user_id}/keys")
def list_keys(user_id: str, actor: Principal = Depends(roles("access_admin")),
              db: Session = Depends(get_db)):
    if not db.get(AccessUser, user_id):
        raise HTTPException(404, "Identity not found")
    values = db.scalars(select(AccessKey).where(
        AccessKey.user_id == user_id
    ).order_by(AccessKey.created_at.desc()))
    return [public_key(x) for x in values]


@router.post("/users/{user_id}/keys", status_code=201)
def create_key(user_id: str, body: NewKey, request: Request,
               actor: Principal = Depends(roles("access_admin")),
               db: Session = Depends(get_db)):
    user = db.get(AccessUser, user_id)
    if not user:
        raise HTTPException(404, "Identity not found")
    if not user.enabled:
        raise HTTPException(409, "Identity is disabled")
    token, key = issue_key(db, user, body.label, actor.name, body.expiresInDays)
    transaction_audit(db, request, actor, 201)
    db.commit()
    return {**public_key(key), "tokenOnce": token}


@router.post("/keys/{key_id}/rotate")
def rotate_key(key_id: str, body: NewKey, request: Request,
               actor: Principal = Depends(roles("access_admin")),
               db: Session = Depends(get_db)):
    old = db.get(AccessKey, key_id, with_for_update=True)
    if not old:
        raise HTTPException(404, "Key not found")
    if old.revoked_at is not None:
        raise HTTPException(409, "Key is already revoked")
    user = db.get(AccessUser, old.user_id)
    if not user or not user.enabled:
        raise HTTPException(409, "Identity is disabled")
    token, key = issue_key(db, user, body.label, actor.name, body.expiresInDays)
    old.revoked_at = utc_now()
    transaction_audit(db, request, actor, 200)
    db.commit()
    return {**public_key(key), "tokenOnce": token,
            "replacedKeyId": old.id}


@router.post("/keys/{key_id}/revoke")
def revoke_key(key_id: str, request: Request,
               actor: Principal = Depends(roles("access_admin")),
               db: Session = Depends(get_db)):
    key = db.get(AccessKey, key_id, with_for_update=True)
    if not key:
        raise HTTPException(404, "Key not found")
    if key.revoked_at is not None:
        raise HTTPException(409, "Key already revoked")
    if actor.key_id == key.id:
        raise HTTPException(409, "Cannot revoke the current administrative key")
    user = db.get(AccessUser, key.user_id)
    if user and user.role == "access_admin" and user.enabled:
        active = db.scalar(select(func.count()).select_from(AccessKey).join(
            AccessUser, AccessKey.user_id == AccessUser.id
        ).where(
            AccessUser.role == "access_admin", AccessUser.enabled.is_(True),
            AccessKey.revoked_at.is_(None),
            AccessKey.expires_at > utc_now(),
            AccessKey.id != key_id,
        )) or 0
        if active == 0:
            raise HTTPException(409, "Cannot revoke the last valid administrative key")
    key.revoked_at = utc_now()
    transaction_audit(db, request, actor, 200)
    db.commit()
    return {"id": key.id, "revoked": True}


@router.get("/audit")
def audit(limit: int = Query(default=100, ge=1, le=500),
          actor: Principal = Depends(roles("access_admin")),
          db: Session = Depends(get_db)):
    values = db.scalars(select(AccessAudit).order_by(
        AccessAudit.id.desc()).limit(limit))
    return [
        {"id": a.id, "requestId": a.request_id,
         "actor": a.actor_name, "role": a.actor_role,
         "method": a.method, "path": a.path,
         "status": a.status_code, "recordedAt": a.recorded_at}
        for a in values
    ]
