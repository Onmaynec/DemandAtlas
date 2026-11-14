"""Dataset state bootstrap and auditable reprocessing metadata.

Revision ID: 0002_data_integrity
Revises: 0001_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_data_integrity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # 0001_initial uses metadata.create_all: on fresh installations that
    # metadata may already expose v0.2 fields. Existing v0.1 databases do
    # not have them. Inspect both paths instead of emitting duplicate DDL.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("pipeline_runs")}
    if "mode" not in columns:
        op.add_column("pipeline_runs", sa.Column(
            "mode", sa.String(length=20), nullable=False, server_default="append"
        ))
    if "scope" not in columns:
        op.add_column("pipeline_runs", sa.Column("scope", sa.JSON(), nullable=True))
    op.execute(
        "INSERT INTO dataset_state (id, version) VALUES (1, 0) "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade():
    op.drop_column("pipeline_runs", "scope")
    op.drop_column("pipeline_runs", "mode")
