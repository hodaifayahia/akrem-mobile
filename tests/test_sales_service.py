"""Customer and sale workflow validation, calculations, and persistence tests."""

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Customer, Installment, Payment, Sale
from app.db.session import session_scope
from app.services import customers, sales
from app.services.seed import seed_defaults


def _new_customer(session: Session) -> Customer:
    """Create a customer in a database with seeded categories."""
    category = session.scalar(select(Category).order_by(Category.id))
    assert category is not None
    return customers.create_customer(
        session,
        full_name="Akrem Mobile Customer",
        category_id=category.id,
        phone="0550123456",
    )


def test_customer_phone_validation_and_duplicate_confirmation(memory_engine: Engine) -> None:
    """Accept Algerian mobile formats, report duplicates, and permit confirmation."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        customer = _new_customer(session)
        category = customer.category

        assert customers.find_duplicate_phones(session, "٠٥٥٠١٢٣٤٥٦") == [customer]
        with pytest.raises(ValueError, match="starting 05, 06, or 07"):
            customers.create_customer(
                session,
                full_name="Bad phone",
                category_id=category.id,
                phone="0812345678",
            )
        with pytest.raises(customers.DuplicateCustomerPhoneError) as duplicate_error:
            customers.create_customer(
                session,
                full_name="Same phone",
                category_id=category.id,
                phone="0550123456",
            )
        assert duplicate_error.value.matches == (customer,)
        duplicate = customers.create_customer(
            session,
            full_name="Same phone",
            category_id=category.id,
            phone="0550123456",
            allow_duplicate_phone=True,
        )
        assert duplicate.phone == customer.phone
        assert customers.find_duplicate_phones(
            session, "0550123456", exclude_customer_id=customer.id
        ) == [duplicate]


def test_customer_update_checks_duplicate_phone(memory_engine: Engine) -> None:
    """Check changed phone numbers and exclude the current customer from matches."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        first = _new_customer(session)
        second = customers.create_customer(
            session,
            full_name="Second customer",
            category_id=first.category_id,
            phone="0612345678",
        )
        with pytest.raises(customers.DuplicateCustomerPhoneError):
            customers.update_customer(session, second.id, phone=first.phone)
        customers.update_customer(session, first.id, phone=first.phone)
        customers.update_customer(
            session, second.id, phone=first.phone, allow_duplicate_phone=True
        )
        assert second.phone == first.phone


def test_create_installment_sale_builds_full_schedule(memory_engine: Engine) -> None:
    """Persist calculated values and first-of-next-month installment dates."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        customer = _new_customer(session)
        sale = sales.create_sale(
            session,
            customer_id=customer.id,
            product="Phone model X",
            sale_type="installment",
            wholesale_price=130_000,
            cash_price=145_000,
            rate=10,
            down_payment=90_000,
            months=3,
            purchase_date=date(2026, 9, 30),
        )
        installments = list(
            session.scalars(
                select(Installment)
                .where(Installment.sale_id == sale.id)
                .order_by(Installment.installment_index)
            )
        )
        assert (sale.total, sale.financed, sale.monthly_amount, sale.profit) == (
            159_500,
            69_500,
            23_166,
            29_500,
        )
        assert sale.end_date == date(2026, 12, 30)
        assert [row.amount_due for row in installments] == [23_166, 23_166, 23_168]
        assert [row.due_date for row in installments] == [
            date(2026, 10, 1),
            date(2026, 11, 1),
            date(2026, 12, 1),
        ]
        assert sum(row.amount_due for row in installments) == sale.financed


def test_cash_and_credit_sales_do_not_create_schedules(memory_engine: Engine) -> None:
    """Store cash totals and credit balance while omitting monthly rows."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        customer = _new_customer(session)
        cash = sales.create_sale(
            session,
            customer_id=customer.id,
            product="Cash phone",
            sale_type="cash",
            wholesale_price=29_900,
            cash_price=32_000,
            purchase_date=date(2026, 9, 30),
        )
        credit = sales.create_sale(
            session,
            customer_id=customer.id,
            product="Credit phone",
            sale_type="credit",
            wholesale_price=50_000,
            cash_price=60_000,
            rate=0,
            down_payment=15_000,
            purchase_date=date(2026, 9, 30),
            expected_pay_date=date(2026, 10, 30),
        )
        assert (cash.total, cash.financed, cash.profit, cash.monthly_amount) == (
            32_000,
            32_000,
            2_100,
            None,
        )
        assert (credit.total, credit.financed, credit.down_payment) == (60_000, 45_000, 15_000)
        assert credit.expected_pay_date == date(2026, 10, 30)
        assert session.scalar(select(func.count()).select_from(Installment)) == 0


def test_sale_and_schedule_roll_back_together_on_schedule_failure(
    memory_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not leave a sale persisted when schedule generation fails."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        customer = _new_customer(session)

        def fail_schedule(*_args: object, **_kwargs: object) -> list[object]:
            raise RuntimeError("schedule calculation failed")

        monkeypatch.setattr(sales.schedule, "build", fail_schedule)
        with pytest.raises(RuntimeError, match="schedule calculation failed"):
            sales.create_sale(
                session,
                customer_id=customer.id,
                product="Phone model X",
                sale_type="installment",
                wholesale_price=30_000,
                cash_price=40_000,
                rate=20,
                months=3,
                purchase_date=date(2026, 9, 30),
            )
        assert session.scalar(select(func.count()).select_from(Sale)) == 0
        assert session.scalar(select(func.count()).select_from(Installment)) == 0


def test_sale_edit_requires_confirmation_and_preserves_paid_rows(memory_engine: Engine) -> None:
    """Warn before rebuilding paid history and retain paid rows after confirmation."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        customer = _new_customer(session)
        sale = sales.create_sale(
            session,
            customer_id=customer.id,
            product="Phone model X",
            sale_type="installment",
            wholesale_price=40_000,
            cash_price=50_000,
            rate=20,
            down_payment=10_000,
            months=6,
            purchase_date=date(2026, 9, 30),
        )
        first = session.scalar(
            select(Installment).where(
                Installment.sale_id == sale.id,
                Installment.installment_index == 1,
            )
        )
        assert first is not None
        first.amount_paid = first.amount_due
        first.paid_date = date(2026, 10, 1)
        first.method = "cash"
        session.add(
            Payment(
                installment_id=first.id,
                amount=first.amount_due,
                payment_date=date(2026, 10, 1),
                method="cash",
            )
        )
        session.flush()
        original_first = (first.due_date, first.amount_due, first.amount_paid, first.paid_date)

        with pytest.raises(sales.PaidInstallmentEditError, match="Confirm the edit"):
            sales.update_sale(session, sale.id, rate=30)
        assert sale.rate == 20

        updated = sales.update_sale(
            session, sale.id, rate=30, allow_paid_history_rewrite=True
        )
        rows = list(
            session.scalars(
                select(Installment)
                .where(Installment.sale_id == sale.id)
                .order_by(Installment.installment_index)
            )
        )
        kept_first = next(row for row in rows if row.installment_index == 1)
        assert (kept_first.due_date, kept_first.amount_due, kept_first.amount_paid, kept_first.paid_date) == original_first
        assert len(rows) == 6
        assert sum(row.amount_due for row in rows) == updated.financed
        assert session.scalar(select(func.count()).select_from(Payment)) == 1


def test_invalid_sale_does_not_persist(memory_engine: Engine) -> None:
    """Reject a down payment above the calculated total."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        customer = _new_customer(session)
        with pytest.raises(ValueError, match="cannot exceed total"):
            sales.create_sale(
                session,
                customer_id=customer.id,
                product="Phone model X",
                sale_type="installment",
                wholesale_price=30_000,
                cash_price=40_000,
                rate=0,
                down_payment=50_000,
                months=3,
                purchase_date=date(2026, 9, 30),
            )
        assert session.scalar(select(func.count()).select_from(Sale)) == 0
