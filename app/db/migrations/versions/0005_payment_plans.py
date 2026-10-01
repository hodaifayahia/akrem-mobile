"""Add flexible installment plans: payment interval on sales, default plan on products.

Revision ID: 0005_payment_plans
Revises: 0004_two_factor
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0005_payment_plans"
down_revision = "0004_two_factor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Existing sales stay monthly; existing products get a 6-month monthly plan."""
    # recreate="never" keeps SQLite on plain ALTER TABLE so no table is copied
    # and foreign keys from installments/payments are untouched (see 0003).
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.add_column(
            sa.Column("payment_interval", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("products", recreate="never") as batch_op:
        batch_op.add_column(
            sa.Column("default_months", sa.Integer(), nullable=False, server_default="6")
        )
        batch_op.add_column(
            sa.Column("payment_interval", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("default_rate", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop the plan columns with plain ALTER TABLE DROP COLUMN (SQLite 3.35+)."""
    with op.batch_alter_table("products", recreate="never") as batch_op:
        batch_op.drop_column("default_rate")
        batch_op.drop_column("payment_interval")
        batch_op.drop_column("default_months")
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.drop_column("payment_interval")
