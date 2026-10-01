"""Add stock items (one row per phone) and product details on sales.

Revision ID: 0006_stock_items
Revises: 0005_payment_plans
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0006_stock_items"
down_revision = "0005_payment_plans"
branch_labels = None
depends_on = None

_SALE_DETAIL_COLUMNS = (
    ("color", "VARCHAR(60)"),
    ("battery_health", "INTEGER"),
    ("imei", "VARCHAR(40)"),
    ("reference", "VARCHAR(40)"),
)


def upgrade() -> None:
    """Create stock_items; add product link and unit details to sales."""
    op.create_table(
        "stock_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("wholesale_price", sa.Integer(), nullable=False),
        sa.Column("cash_price", sa.Integer(), nullable=False),
        sa.Column("color", sa.String(length=60), nullable=True),
        sa.Column("battery_health", sa.Integer(), nullable=True),
        sa.Column("is_new", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("imei", sa.String(length=40), nullable=True, unique=True),
        sa.Column("reference", sa.String(length=40), nullable=True, unique=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="available"),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sales.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sold_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("status IN ('available', 'sold')", name="ck_stock_items_status"),
        sa.CheckConstraint("wholesale_price >= 0", name="ck_stock_items_wholesale_nonnegative"),
        sa.CheckConstraint("cash_price >= 0", name="ck_stock_items_cash_nonnegative"),
        sa.CheckConstraint(
            "battery_health IS NULL OR (battery_health >= 0 AND battery_health <= 100)",
            name="ck_stock_items_battery_range",
        ),
    )
    op.create_index("ix_stock_items_product_id", "stock_items", ["product_id"])
    op.create_index("ix_stock_items_status", "stock_items", ["status"])
    op.create_index("ix_stock_items_sale_id", "stock_items", ["sale_id"])

    if op.get_bind().dialect.name == "sqlite":
        # Plain ALTER TABLE: a batch copy would DROP sales, and with foreign keys
        # on that would cascade into installments and payments (see 0003).
        op.execute(
            "ALTER TABLE sales ADD COLUMN product_id INTEGER REFERENCES products (id)"
        )
        for name, sql_type in _SALE_DETAIL_COLUMNS:
            op.execute(f"ALTER TABLE sales ADD COLUMN {name} {sql_type}")
        op.execute("ALTER TABLE sales ADD COLUMN is_new BOOLEAN NOT NULL DEFAULT 0")
    else:
        op.add_column("sales", sa.Column("product_id", sa.Integer(), nullable=True))
        op.create_foreign_key(None, "sales", "products", ["product_id"], ["id"])
        op.add_column("sales", sa.Column("color", sa.String(length=60), nullable=True))
        op.add_column("sales", sa.Column("battery_health", sa.Integer(), nullable=True))
        op.add_column("sales", sa.Column("imei", sa.String(length=40), nullable=True))
        op.add_column("sales", sa.Column("reference", sa.String(length=40), nullable=True))
        op.add_column("sales", sa.Column("is_new", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_index("ix_sales_product_id", "sales", ["product_id"])
    # Link existing sales to catalog products with exactly the same name.
    op.execute(
        "UPDATE sales SET product_id = (SELECT products.id FROM products WHERE products.name = sales.product)"
    )


def downgrade() -> None:
    """Drop the stock table and the sale detail columns (SQLite 3.35+ DROP COLUMN)."""
    op.drop_index("ix_sales_product_id", table_name="sales")
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        for constraint in sa.inspect(bind).get_foreign_keys("sales"):
            if constraint["referred_table"] == "products" and constraint.get("name"):
                op.drop_constraint(constraint["name"], "sales", type_="foreignkey")
    for name in ("is_new", "reference", "imei", "battery_health", "color", "product_id"):
        op.execute(f"ALTER TABLE sales DROP COLUMN {name}")
    op.drop_index("ix_stock_items_sale_id", table_name="stock_items")
    op.drop_index("ix_stock_items_status", table_name="stock_items")
    op.drop_index("ix_stock_items_product_id", table_name="stock_items")
    op.drop_table("stock_items")
