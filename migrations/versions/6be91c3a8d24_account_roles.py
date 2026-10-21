"""Website roles, delegated grants, and account revisions.

Revision ID: 6be91c3a8d24
Revises: 3ac46f912d08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "6be91c3a8d24"
down_revision = "3ac46f912d08"
branch_labels = None
depends_on = None


def upgrade():
    # Every existing login was an administrator. Preserve its access explicitly,
    # then make the database default for future accounts the least-privileged role.
    op.add_column("admins", sa.Column("role", sa.String(10), nullable=False, server_default="admin"))
    op.alter_column("admins", "role", server_default="user")
    op.add_column(
        "admins",
        sa.Column(
            "permissions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[\"read\"]'::jsonb")
        ),
    )
    op.add_column(
        "admins", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"))
    )
    op.add_column("admins", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_account_role", "admins", "role IN ('admin', 'user')")


def downgrade():
    # Refuse a downgrade that would silently turn User accounts into legacy admins.
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM admins WHERE role <> 'admin' OR NOT enabled)")
    ).scalar():
        raise RuntimeError("Remove User/disabled accounts before downgrading to admin-only authentication")
    op.drop_constraint("ck_account_role", "admins", type_="check")
    for column in ("revision", "enabled", "permissions", "role"):
        op.drop_column("admins", column)
