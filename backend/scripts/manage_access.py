"""Offline bootstrap of the first administrator.

Examples:
  python -m backend.scripts.manage_access bootstrap --name operator

Requires the Alembic schema to be upgraded. The CLI refuses to create
another bootstrap account once any identity already exists.
"""
import argparse
import sys
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.app.auth import issue_key, utc_now
from backend.app.db import SessionLocal
from backend.app.models import AccessAudit, AccessKey, AccessUser


def bootstrap(name: str, expires_in_days=90):
    if not name or len(name) > 70 or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        for c in name
    ):
        raise ValueError("Name must be 1-70 ASCII letters, digits, _, -, or .")
    with SessionLocal.begin() as db:
        if db.scalar(select(func.count()).select_from(AccessUser)):
            raise RuntimeError(
                "An identity already exists. Use the administrator API to create users."
            )
        user = AccessUser(
            id=str(uuid4()), name=name, role="access_admin",
            stores=[], categories=[], enabled=True,
        )
        db.add(user)
        # Flush so the FK exists on all SQLAlchemy configurations.
        db.flush()
        token, key = issue_key(db, user, "bootstrap", "local-bootstrap",
                               expires_in_days)
        db.flush()
        return token, key.id



def recover(name: str, expires_in_days=30):
    """Issue a break-glass key only if NO valid admin credentials remain.

    This is an offline database-privileged operation, not an HTTP endpoint.
    """
    with SessionLocal.begin() as db:
        alive = db.scalar(select(func.count()).select_from(AccessKey).join(
            AccessUser, AccessUser.id == AccessKey.user_id
        ).where(
            AccessUser.role == "access_admin", AccessUser.enabled.is_(True),
            AccessKey.revoked_at.is_(None), AccessKey.expires_at > utc_now()
        )) or 0
        if alive:
            raise RuntimeError(
                "An active administrator key exists. Recovery is not permitted."
            )
        user = db.scalar(select(AccessUser).where(
            AccessUser.name == name, AccessUser.role == "access_admin"
        ).with_for_update())
        if user is None:
            raise ValueError("No existing administrator with this name")
        user.enabled = True
        user.updated_at = utc_now()
        db.flush()
        token, key = issue_key(db, user, "offline-recovery",
                               "local-breakglass", expires_in_days)
        db.flush()
        db.add(AccessAudit(
            request_id="offline-" + str(uuid4()),
            actor_id=user.id,
            actor_name="local-breakglass", actor_role="offline-operator",
            method="CLI", path="/admin/recover",
            status_code=201,
        ))
        return token, key.id


def main():
    parser = argparse.ArgumentParser(description="DemandAtlas access management")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("bootstrap", help="Create the first access admin")
    start.add_argument("--name", required=True)
    start.add_argument("--expires-in-days", type=int, default=90)
    recovery = commands.add_parser("recover", help="Break glass if every admin key expired")
    recovery.add_argument("--name", required=True)
    recovery.add_argument("--expires-in-days", type=int, default=30)
    args = parser.parse_args()
    try:
        if args.command == "recover":
            secret, key_id = recover(args.name, args.expires_in_days)
        else:
            secret, key_id = bootstrap(args.name, args.expires_in_days)
    except (ValueError, RuntimeError, IntegrityError) as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Key id: {key_id}")
    print("The following Bearer token is shown ONCE; store it securely.")
    print(secret)


if __name__ == "__main__":
    main()
