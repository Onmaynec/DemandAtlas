"""Database identities, key revocation, audit trail and distributed rate buckets.

Revision ID: 0003_access_control
Revises: 0002_data_integrity

0001_initial creates current Base.metadata on fresh databases. checkfirst
protects fresh installations and also upgrades real older databases.
"""
from alembic import op
from backend.app.models import AccessUser, AccessKey, AccessAudit, RateLimitWindow

revision = "0003_access_control"
down_revision = "0002_data_integrity"
branch_labels = None
depends_on = None

TABLES = (AccessUser.__table__, AccessKey.__table__,
          AccessAudit.__table__, RateLimitWindow.__table__)


def upgrade():
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind=bind, checkfirst=True)
