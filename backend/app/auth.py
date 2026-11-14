import os
import secrets
from dataclasses import dataclass
from fastapi import Depends, Header, HTTPException, status


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    stores: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


def principals():
    demo = os.getenv("DEMO_MODE", "0") == "1"
    profiles = [
        ("engineer", "data_engineer", (), ()),
        ("analyst", "analyst", (), ()),
        ("category", "category_manager", (), ("Food",)),
        ("director", "director", (), ()),
        ("observer", "observer", ("S001", "S002"), ()),
    ]
    output = {}
    for name, role, stores, categories in profiles:
        token = os.getenv("TOKEN_" + name.upper())
        if demo and not token:
            token = "demo-" + name
        if token:
            output[token] = Principal(name, role, stores, categories)
    return output


def current_user(authorization: str | None = Header(default=None)) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    incoming = authorization[7:]
    for token, user in principals().items():
        if secrets.compare_digest(token, incoming):
            return user
    raise HTTPException(status_code=401, detail="Invalid credentials")


def roles(*allowed: str):
    def check(user: Principal = Depends(current_user)):
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return user
    return check


def can_access(user: Principal, store: str | None = None, category: str | None = None) -> bool:
    return ((not user.stores or store in user.stores or store is None)
            and (not user.categories or category in user.categories or category is None))


def require_scope(user: Principal, store: str | None = None, category: str | None = None):
    if not can_access(user, store, category):
        raise HTTPException(status_code=403, detail="Entity is outside permitted scope")
