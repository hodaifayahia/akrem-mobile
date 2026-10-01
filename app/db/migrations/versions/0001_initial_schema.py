"""Create initial AkremMobile tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create initial tables and indexes."""
    op.create_table(
        "categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False, unique=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.String(length=180), nullable=False),
        sa.Column("phone", sa.String(length=20)),
        sa.Column("id_number", sa.String(length=60)),
        sa.Column("id_issue_date", sa.Date()),
        sa.Column("id_issue_place", sa.String(length=120)),
        sa.Column("address", sa.String(length=240)),
        sa.Column("profession", sa.String(length=120)),
        sa.Column("ccp_number", sa.String(length=60)),
        sa.Column("cheques_count", sa.Integer()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_customers_full_name", "customers", ["full_name"])
    op.create_index("ix_customers_phone", "customers", ["phone"])

    op.create_table(
        "sales",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("product", sa.String(length=180), nullable=False),
        sa.Column("sale_type", sa.String(length=20), nullable=False),
        sa.Column("wholesale_price", sa.Integer(), nullable=False),
        sa.Column("cash_price", sa.Integer(), nullable=False),
        sa.Column("rate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("down_payment", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("months", sa.Integer()),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("financed", sa.Integer(), nullable=False),
        sa.Column("monthly_amount", sa.Integer()),
        sa.Column("profit", sa.Integer(), nullable=False),
        sa.Column("purchase_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date()),
        sa.Column("expected_pay_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("sale_type IN ('cash', 'installment', 'credit')", name="ck_sales_type"),
        sa.CheckConstraint("wholesale_price >= 0", name="ck_sales_wholesale_nonnegative"),
        sa.CheckConstraint("cash_price >= 0", name="ck_sales_cash_nonnegative"),
        sa.CheckConstraint("down_payment >= 0", name="ck_sales_down_payment_nonnegative"),
        sa.CheckConstraint("rate >= 0 AND rate <= 100", name="ck_sales_rate_range"),
        sa.CheckConstraint("months IS NULL OR months >= 1", name="ck_sales_months_positive"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_sales_customer_id", "sales", ["customer_id"])
    op.create_index("ix_sales_purchase_date", "sales", ["purchase_date"])

    op.create_table(
        "installments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sale_id", sa.Integer(), nullable=False),
        sa.Column("installment_index", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("amount_due", sa.Integer(), nullable=False),
        sa.Column("amount_paid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paid_date", sa.Date()),
        sa.Column("method", sa.String(length=30)),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint("installment_index >= 1", name="ck_installments_index_positive"),
        sa.CheckConstraint("amount_due >= 0 AND amount_paid >= 0",
                           name="ck_installments_amounts_nonnegative"),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("sale_id", "installment_index", name="uq_installments_sale_index"),
    )
    op.create_index("ix_installments_due_date", "installments", ["due_date"])
    op.create_index("ix_installments_sale_id", "installments", ["sale_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("installment_id", sa.Integer()),
        sa.Column("sale_id", sa.Integer()),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=30), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint(
            "(installment_id IS NOT NULL AND sale_id IS NULL) OR "
            "(installment_id IS NULL AND sale_id IS NOT NULL)",
            name="ck_payments_exactly_one_target",
        ),
        sa.ForeignKeyConstraint(["installment_id"], ["installments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sale_id"], ["sales.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_payments_installment_id", "payments", ["installment_id"])
    op.create_index("ix_payments_payment_date", "payments", ["payment_date"])
    op.create_index("ix_payments_sale_id", "payments", ["sale_id"])

    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="seller"),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("role IN ('owner', 'seller')", name="ck_users_role"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer()),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("details", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity", "entity_id"])


def downgrade() -> None:
    """Drop initial tables in dependency order."""
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
    op.drop_table("settings")
    op.drop_index("ix_payments_payment_date", table_name="payments")
    op.drop_index("ix_payments_sale_id", table_name="payments")
    op.drop_index("ix_payments_installment_id", table_name="payments")
    op.drop_table("payments")
    op.drop_index("ix_installments_sale_id", table_name="installments")
    op.drop_index("ix_installments_due_date", table_name="installments")
    op.drop_table("installments")
    op.drop_index("ix_sales_purchase_date", table_name="sales")
    op.drop_index("ix_sales_customer_id", table_name="sales")
    op.drop_table("sales")
    op.drop_index("ix_customers_phone", table_name="customers")
    op.drop_index("ix_customers_full_name", table_name="customers")
    op.drop_table("customers")
    op.drop_table("categories")
