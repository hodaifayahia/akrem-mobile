"""Products bought together share a basket.

* ``sales.basket_id``: the sales saved together from one basket share it, so
  the client list shows them on one row. Existing sales keep NULL (each its
  own row, as before).

Revision ID: 0009_sale_baskets
Revises: 0008_quantity_flexible_payments
Create Date: 2026-10-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0009_sale_baskets"
down_revision = "0008_quantity_flexible_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable basket id and its index."""
    # recreate="never" keeps SQLite on plain ALTER TABLE (see 0003 and 0005).
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("basket_id", sa.String(length=32), nullable=True))
    op.create_index("ix_sales_basket_id", "sales", ["basket_id"])


def downgrade() -> None:
    """Drop the index and the column."""
    op.drop_index("ix_sales_basket_id", table_name="sales")
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.drop_column("basket_id")
