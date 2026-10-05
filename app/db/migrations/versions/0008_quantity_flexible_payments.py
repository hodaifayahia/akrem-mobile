"""Sale quantity, grouped flexible payments, and due dates on the purchase day.

* ``sales.quantity``: how many stock units a sale took (existing sales: 1).
* ``payments.batch_id``: the parts of one amount spread over several
  installments share it, so the amount is undone as one payment.
* Installments now fall due on the purchase day of the month (a sale on
  02/02/2026 paid monthly is due on 02/03/2026) instead of the 1st. The
  stored ``due_mode`` setting is switched and the due dates generated on the
  1st are moved to the purchase day; any other date is left alone.

Revision ID: 0008_quantity_flexible_payments
Revises: 0007_debts
Create Date: 2026-10-02
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

from alembic import op
import sqlalchemy as sa

revision = "0008_quantity_flexible_payments"
down_revision = "0007_debts"
branch_labels = None
depends_on = None

_sales = sa.table(
    "sales",
    sa.column("id", sa.Integer),
    sa.column("sale_type", sa.String),
    sa.column("purchase_date", sa.Date),
    sa.column("months", sa.Integer),
    sa.column("payment_interval", sa.Integer),
)
_installments = sa.table(
    "installments",
    sa.column("id", sa.Integer),
    sa.column("sale_id", sa.Integer),
    sa.column("installment_index", sa.Integer),
    sa.column("due_date", sa.Date),
)
_settings = sa.table("settings", sa.column("key", sa.String), sa.column("value", sa.Text))


def upgrade() -> None:
    """Add the columns, then move first-of-month due dates to the purchase day."""
    # recreate="never" keeps SQLite on plain ALTER TABLE (see 0003 and 0005).
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"))
    with op.batch_alter_table("payments", recreate="never") as batch_op:
        batch_op.add_column(sa.Column("batch_id", sa.String(length=32), nullable=True))

    bind = op.get_bind()
    bind.execute(
        _settings.update()
        .where(_settings.c.key == "due_mode", _settings.c.value == '"first_of_month"')
        .values(value='"purchase_day"')
    )
    _move_due_dates_to_purchase_day(bind)


def downgrade() -> None:
    """Drop the columns; due dates stay where they are."""
    with op.batch_alter_table("payments", recreate="never") as batch_op:
        batch_op.drop_column("batch_id")
    with op.batch_alter_table("sales", recreate="never") as batch_op:
        batch_op.drop_column("quantity")


def _move_due_dates_to_purchase_day(bind: sa.engine.Connection) -> None:
    sales = bind.execute(
        sa.select(_sales.c.id, _sales.c.purchase_date, _sales.c.months, _sales.c.payment_interval)
        .where(_sales.c.sale_type == "installment", _sales.c.months.is_not(None))
    ).all()
    for sale_id, purchase_date, months, interval in sales:
        if purchase_date is None or not months or months < 1:
            continue
        offsets = _payment_offsets(months, min(max(interval or 1, 1), months))
        rows = bind.execute(
            sa.select(_installments.c.id, _installments.c.installment_index, _installments.c.due_date)
            .where(_installments.c.sale_id == sale_id)
        ).all()
        for row_id, index, due_date in rows:
            if not 1 <= index <= len(offsets):
                continue
            target = _add_months(purchase_date, offsets[index - 1])
            if due_date == target.replace(day=1) and due_date != target:
                bind.execute(
                    _installments.update().where(_installments.c.id == row_id).values(due_date=target)
                )


# Copies of the calculation helpers, so this migration keeps working if they change.
def _add_months(value: date, months: int) -> date:
    month_index = value.year * 12 + (value.month - 1) + months
    year, zero_month = divmod(month_index, 12)
    month = zero_month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _payment_offsets(months: int, interval: int) -> list[int]:
    offsets = list(range(interval, months + 1, interval))
    if not offsets or offsets[-1] != months:
        offsets.append(months)
    return offsets
