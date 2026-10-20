"""A product the client brought back.

* ``sales.returned_at``, ``sales.refund_amount``, ``sales.return_reason``:
  set when the owner records a return (``sales.return_sales``). Existing
  sales keep NULL (not returned).

Revision ID: 0010_sale_returns
Revises: 0009_sale_baskets
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0010_sale_returns"
down_revision = "0009_sale_baskets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the three nullable return columns."""
    # recreate="never" keeps SQLite on plain ALTER TABLE (see 0003 and 0005).
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("returned_at", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("refund_amount", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("return_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the return columns."""
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.drop_column("return_reason")
        batch_op.drop_column("refund_amount")
        batch_op.drop_column("returned_at")
