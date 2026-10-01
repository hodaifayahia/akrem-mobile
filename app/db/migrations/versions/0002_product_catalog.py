"""Create the owner-managed product price book.

Revision ID: 0002_product_catalog
Revises: 0001_initial
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_product_catalog"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add products without changing existing sale price snapshots."""
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=180), nullable=False, unique=True),
        sa.Column("wholesale_price", sa.Integer(), nullable=False),
        sa.Column("cash_price", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("wholesale_price >= 0", name="ck_products_wholesale_nonnegative"),
        sa.CheckConstraint("cash_price >= 0", name="ck_products_cash_nonnegative"),
    )


def downgrade() -> None:
    """Remove catalog entries; sales retain their own historical price fields."""
    op.drop_table("products")
