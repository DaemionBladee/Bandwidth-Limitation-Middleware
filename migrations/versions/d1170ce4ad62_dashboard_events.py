"""Notify dashboard listeners after committed data changes.

Revision ID: d1170ce4ad62
Revises: ed86e8ce1df6
"""

from alembic import op

revision = "d1170ce4ad62"
down_revision = "ed86e8ce1df6"
branch_labels = None
depends_on = None

TABLES = (
    "users",
    "policies",
    "policy_versions",
    "assignments",
    "quota_periods",
    "usage_resets",
    "accounting_records",
    "enforcement_states",
    "admin_audit",
    "accounting_checkpoints",
    "jobs",
    "admin_sessions",
    "admins",
)


def upgrade():
    op.execute("""
        CREATE FUNCTION bandwidth_dashboard_changed() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('bandwidth_dashboard', 'changed');
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    for table in TABLES:
        op.execute(f"""CREATE TRIGGER dashboard_changed AFTER INSERT OR UPDATE OR DELETE ON {table}
                       FOR EACH STATEMENT EXECUTE FUNCTION bandwidth_dashboard_changed()""")


def downgrade():
    for table in TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS dashboard_changed ON {table}")
    op.execute("DROP FUNCTION bandwidth_dashboard_changed()")
