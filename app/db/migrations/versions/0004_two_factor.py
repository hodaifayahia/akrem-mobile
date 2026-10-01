"""Add two-step verification (TOTP) columns to users.

Revision ID: 0004_two_factor
Revises: 0003_client_types
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0004_two_factor"
down_revision = "0003_client_types"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the TOTP secret, enabled flag, and last accepted time-step counter."""
    # recreate="never" keeps SQLite on plain ALTER TABLE: a batch table copy would
    # DROP the users table and trip the audit_logs foreign key (see 0003).
    with op.batch_alter_table("users", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("totp_secret", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("totp_last_counter", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the TOTP columns with plain ALTER TABLE DROP COLUMN (SQLite 3.35+)."""
    with op.batch_alter_table("users", recreate="never") as batch_op:
        batch_op.drop_column("totp_last_counter")
        batch_op.drop_column("totp_enabled")
        batch_op.drop_column("totp_secret")
