"""Configurable default plan for first-time subscribers.

Revision ID: 3ac46f912d08
Revises: d1170ce4ad62
"""

import sqlalchemy as sa
from alembic import op

revision = "3ac46f912d08"
down_revision = "d1170ce4ad62"
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table(
        "default_plan",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("policy_id", sa.Integer(), sa.ForeignKey("policies.id"), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.CheckConstraint("id = 1"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(table, [{"id": 1, "policy_id": None, "revision": 0}])
    op.execute("""CREATE TRIGGER dashboard_changed AFTER INSERT OR UPDATE OR DELETE ON default_plan
                   FOR EACH STATEMENT EXECUTE FUNCTION bandwidth_dashboard_changed()""")


def downgrade():
    op.drop_table("default_plan")
