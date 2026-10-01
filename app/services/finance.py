"""The shop's net financial position for the dashboard (owner only).

"Inside" (money and goods that belong to the shop but are not cash yet):

* what clients still owe on installment and credit sales;
* what people owe in the debt book (``receivable``);
* unsold phones in stock, at their wholesale (purchase) price.

"Outside" (what the shop must pay): the debt book's ``payable`` side.

``net = clients + debtors + stock - what the shop owes``. For the selected
month it also gives the money that came in (sales collections and debt
repayments received) and the money paid out on the shop's own debts.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import StockItem
from app.services import auth, dashboard, debts, stock


@dataclass(frozen=True)
class FinancialPosition:
    """Everything the shop has outside its cash drawer, and everything it owes."""

    clients_owe: int
    debtors_owe: int
    stock_value: int
    stock_retail_value: int
    stock_units: int
    shop_owes: int
    month_sales_received: int
    month_debts_received: int
    month_paid_out: int
    debtors_overdue: int
    shop_overdue: int

    @property
    def money_inside(self) -> int:
        """Money owed to the shop (clients and debtors)."""
        return self.clients_owe + self.debtors_owe

    @property
    def net(self) -> int:
        """What the shop is worth outside its cash: owed to it plus stock, minus what it owes."""
        return self.money_inside + self.stock_value - self.shop_owes

    @property
    def month_received(self) -> int:
        return self.month_sales_received + self.month_debts_received

    @property
    def month_net(self) -> int:
        """Cash in minus cash out for the month."""
        return self.month_received - self.month_paid_out


def financial_position(
    session: Session, owner_user_id: int, *, year: int, month: int, today: date
) -> FinancialPosition:
    """Compute the position as of ``today`` and the cash flow of ``year``/``month``."""
    auth.require_owner(session, owner_user_id)
    summary = dashboard.get_dashboard_summary(session, year=year, month=month, today=today, role="owner")
    owed_to_me = debts.totals(session, debts.RECEIVABLE, today=today)
    i_owe = debts.totals(session, debts.PAYABLE, today=today)
    stock_row = session.execute(
        select(
            func.count(StockItem.id),
            func.coalesce(func.sum(StockItem.wholesale_price), 0),
            func.coalesce(func.sum(StockItem.cash_price), 0),
        ).where(StockItem.status == stock.STATUS_AVAILABLE)
    ).one()
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return FinancialPosition(
        clients_owe=summary.remaining_balance,
        debtors_owe=owed_to_me.remaining,
        stock_value=int(stock_row[1]),
        stock_retail_value=int(stock_row[2]),
        stock_units=int(stock_row[0]),
        shop_owes=i_owe.remaining,
        month_sales_received=summary.collected_this_month,
        month_debts_received=debts.payments_between(session, debts.RECEIVABLE, start, end),
        month_paid_out=debts.payments_between(session, debts.PAYABLE, start, end),
        debtors_overdue=owed_to_me.overdue,
        shop_overdue=i_owe.overdue,
    )
