"""Dashboard totals and seller data restrictions."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.db.models import Category, Customer, Installment, Payment, Sale
from app.db.session import session_scope
from app.services import repositories
from app.services.dashboard import get_dashboard_summary
from app.services.seed import seed_defaults


def _sale(
    session,
    customer_id: int,
    name: str,
    *,
    wholesale: int,
    profit: int,
) -> Sale:
    """Create a one-month installment sale with test totals."""
    return repositories.create_sale(
        session,
        customer_id=customer_id,
        product=name,
        sale_type="installment",
        wholesale_price=wholesale,
        cash_price=wholesale + profit,
        rate=0,
        down_payment=0,
        months=1,
        total=wholesale + profit,
        financed=wholesale + profit,
        monthly_amount=wholesale + profit,
        profit=profit,
        purchase_date=date(2026, 9, 1),
        end_date=date(2026, 10, 1),
    )


def test_dashboard_status_counts_and_owner_values(memory_engine: Engine) -> None:
    """Count sale operations by status and compute monthly collections."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        category = session.scalar(select(Category).where(Category.name == "أساتذة"))
        assert category is not None
        customer = repositories.create_customer(
            session,
            full_name="Dashboard Test",
            category_id=category.id,
            phone="0550000001",
        )
        sales = [
            _sale(session, customer.id, "Paid", wholesale=50, profit=10),
            _sale(session, customer.id, "Failed", wholesale=60, profit=20),
            _sale(session, customer.id, "Pending", wholesale=70, profit=30),
        ]
        paid_row = Installment(
            sale_id=sales[0].id,
            installment_index=1,
            due_date=date(2026, 10, 5),
            amount_due=60,
            amount_paid=60,
        )
        failed_row = Installment(
            sale_id=sales[1].id,
            installment_index=1,
            due_date=date(2026, 10, 1),
            amount_due=80,
        )
        pending_row = Installment(
            sale_id=sales[2].id,
            installment_index=1,
            due_date=date(2026, 10, 20),
            amount_due=100,
        )
        session.add_all((paid_row, failed_row, pending_row))
        session.flush()
        session.add(Payment(
            installment_id=paid_row.id,
            amount=60,
            payment_date=date(2026, 10, 6),
            method="cash",
        ))
        session.flush()

        summary = get_dashboard_summary(
            session,
            year=2026,
            month=10,
            today=date(2026, 10, 7),
            role="owner",
        )

        assert summary.total_wholesale == 180
        assert summary.failed_operations == 1
        assert summary.completed_operations == 1
        assert summary.pending_operations == 1
        assert summary.expected_collections == 240
        assert summary.collected_this_month == 60
        assert summary.total_profit == 60
        assert summary.remaining_balance == 180
        assert summary.collection_rate == 25  # 60 / 240 * 100
        assert len(summary.monthly_trends) == 6
        assert summary.monthly_trends[-1][0] == "أكتوبر"


def test_dashboard_hides_owner_only_values_from_seller(memory_engine: Engine) -> None:
    """Seller dashboard responses omit wholesale and profit totals."""
    with session_scope(memory_engine) as session:
        summary = get_dashboard_summary(
            session,
            year=2026,
            month=10,
            today=date(2026, 10, 7),
            role="seller",
        )
        assert summary.total_wholesale is None
        assert summary.total_profit is None


def test_dashboard_visual_analytics_widgets():
    """Verify that FinancialTrendChart, CollectionGaugeWidget, and DailyRegisterCard construct cleanly."""
    from PySide6.QtWidgets import QApplication
    from app.ui.widgets.collection_gauge import CollectionGaugeWidget
    from app.ui.widgets.daily_register_card import DailyRegisterCard
    from app.ui.widgets.financial_chart import FinancialTrendChart

    _app = QApplication.instance() or QApplication([])

    chart = FinancialTrendChart()
    chart.set_data([("أوت", 100000, 95000), ("سبتمبر", 120000, 110000), ("أكتوبر", 150000, 140000)])
    assert chart is not None

    gauge = CollectionGaugeWidget()
    gauge.set_values(rate=88, collected=140000, expected=150000)
    assert gauge._rate == 88

    reg = DailyRegisterCard(is_owner=True)
    reg.update_metrics(collected=45000, sales_count=3, profit=12000)
    assert "45,000" in reg.collected_tile[1].text()
    assert "3 عملية" in reg.count_tile[1].text()



def test_cash_sales_do_not_inflate_the_collection_rate(memory_engine: Engine) -> None:
    """A cash sale counts as money collected but not toward installment collection."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        category = session.scalar(select(Category).where(Category.name == "أساتذة"))
        customer = Customer(full_name="زبون", category_id=category.id)
        session.add(customer)
        session.flush()
        cash = Sale(
            customer_id=customer.id, product="Cash phone", sale_type="cash", wholesale_price=10,
            cash_price=500, total=500, financed=0, profit=490, purchase_date=date(2026, 10, 2),
        )
        installment = Sale(
            customer_id=customer.id, product="Installment phone", sale_type="installment",
            wholesale_price=10, cash_price=100, total=100, financed=100, months=1,
            monthly_amount=100, profit=90, purchase_date=date(2026, 9, 2),
        )
        session.add_all((cash, installment))
        session.flush()
        session.add(Installment(sale_id=installment.id, installment_index=1,
                                due_date=date(2026, 10, 1), amount_due=100))
        session.flush()
        summary = get_dashboard_summary(session, year=2026, month=10, today=date(2026, 10, 3), role="owner")
        assert summary.collected_this_month == 500
        assert summary.collection_rate == 0
        assert summary.month_cash_sales == 1
        assert summary.cash_customers == 0 and summary.facility_customers == 1
