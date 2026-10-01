"""Add the debt book: people, debts owed to / by the shop, and their payments.

Revision ID: 0007_debts
Revises: 0006_stock_items
Create Date: 2026-10-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0007_debts"
down_revision = "0006_stock_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create debt_parties, debts and debt_payments."""
    op.create_table(
        "debt_parties",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("first_name", sa.String(length=80), nullable=False),
        sa.Column("last_name", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("national_id", sa.String(length=30), nullable=True, unique=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        "debts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("party_id", sa.Integer(), sa.ForeignKey("debt_parties.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("direction", sa.String(length=12), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("debt_date", sa.Date(), nullable=False),
        sa.Column("plan", sa.String(length=16), nullable=False, server_default="single"),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("months", sa.Integer(), nullable=True),
        sa.Column("payment_interval", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("direction IN ('receivable', 'payable')", name="ck_debts_direction"),
        sa.CheckConstraint("plan IN ('single', 'installments')", name="ck_debts_plan"),
        sa.CheckConstraint("amount > 0", name="ck_debts_amount_positive"),
        sa.CheckConstraint("months IS NULL OR months >= 1", name="ck_debts_months_positive"),
        sa.CheckConstraint("payment_interval >= 1", name="ck_debts_interval_positive"),
    )
    op.create_index("ix_debts_party_id", "debts", ["party_id"])
    op.create_index("ix_debts_direction", "debts", ["direction"])
    op.create_table(
        "debt_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("debt_id", sa.Integer(), sa.ForeignKey("debts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False, server_default="cash"),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("amount > 0", name="ck_debt_payments_amount_positive"),
    )
    op.create_index("ix_debt_payments_debt_id", "debt_payments", ["debt_id"])


def downgrade() -> None:
    """Drop the debt book."""
    op.drop_index("ix_debt_payments_debt_id", table_name="debt_payments")
    op.drop_table("debt_payments")
    op.drop_index("ix_debts_direction", table_name="debts")
    op.drop_index("ix_debts_party_id", table_name="debts")
    op.drop_table("debts")
    op.drop_table("debt_parties")
