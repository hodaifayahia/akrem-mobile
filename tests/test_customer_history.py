"""Customer purchase history and customer-with-first-sale creation."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Customer, Installment, Sale, User
from app.db.session import session_scope
from app.services import auth, customers, payments, products, sales, settings
from app.services.status import Status

TODAY = date(2026, 10, 15)
PASSWORD = "secret1"


def _category(session: Session) -> Category:
    """Create a customer category."""
    category = Category(name="أساتذة")
    session.add(category)
    session.flush()
    return category


def _mixed_customer(session: Session) -> Customer:
    """Customer with a cash, an installment, and a credit sale plus payments.

    Payments are recorded out of date order so ordering by date then id is tested.
    """
    category = _category(session)
    customer = customers.create_customer(
        session, full_name="سمير بن علي", category_id=category.id, phone="0550000001"
    )
    sales.create_sale(
        session, customer_id=customer.id, product="سماعة", sale_type="cash",
        wholesale_price=2_000, cash_price=3_000, purchase_date=date(2026, 1, 10),
    )
    installment_sale = sales.create_sale(
        session, customer_id=customer.id, product="هاتف A15", sale_type="installment",
        wholesale_price=25_000, cash_price=30_000, rate=35, down_payment=5_000, months=4,
        purchase_date=date(2026, 6, 5),
    )
    credit_sale = sales.create_sale(
        session, customer_id=customer.id, product="لوحة", sale_type="credit",
        wholesale_price=15_000, cash_price=20_000, rate=10, down_payment=2_000,
        purchase_date=date(2026, 9, 20), expected_pay_date=date(2026, 12, 1),
    )
    rows = session.scalars(
        select(Installment)
        .where(Installment.sale_id == installment_sale.id)
        .order_by(Installment.installment_index)
    ).all()
    payments.record_credit_payment(session, credit_sale.id, 5_000, date(2026, 10, 2), "cash")
    payments.record_installment_payment(session, rows[0].id, 8_875, date(2026, 7, 2), "CCP")
    payments.record_installment_payment(
        session, rows[1].id, 4_000, date(2026, 8, 3), "BaridiMob", "دفعة جزئية"
    )
    payments.record_credit_payment(session, credit_sale.id, 3_000, date(2026, 10, 2), "cash")
    return customer


def test_history_of_mixed_sales(memory_engine: Engine) -> None:
    """Paid, remaining, status, overdue counts, and totals for each sale type."""
    with session_scope(memory_engine) as session:
        customer = _mixed_customer(session)
        history = customers.customer_history(session, customer.id, today=TODAY)

        assert history.customer_id == customer.id
        assert history.full_name == "سمير بن علي"
        assert [purchase.sale_type for purchase in history.purchases] == [
            "credit", "installment", "cash",
        ]
        credit, installment, cash = history.purchases

        assert (cash.total, cash.paid, cash.remaining, cash.profit) == (3_000, 3_000, 0, 1_000)
        assert cash.status == Status.NONE and cash.overdue_installments == 0
        assert cash.months is None and cash.monthly_amount is None

        assert installment.total == 40_500
        assert installment.monthly_amount == 8_875
        assert installment.months == 4 and installment.rate == 35
        assert installment.end_date == date(2026, 10, 5)
        assert installment.paid == 5_000 + 8_875 + 4_000
        assert installment.remaining == 40_500 - 17_875
        assert installment.profit == 15_500
        assert installment.status == Status.FAILED
        assert installment.overdue_installments == 3  # partial #2, unpaid #3 and #4

        assert credit.total == 22_000 and credit.down_payment == 2_000
        assert credit.paid == 2_000 + 8_000 and credit.remaining == 12_000
        assert credit.expected_pay_date == date(2026, 12, 1)
        assert credit.status == Status.NONE and credit.overdue_installments == 0

        assert history.purchase_count == 3
        assert history.total_bought == 65_500
        assert history.total_paid == 3_000 + 17_875 + 10_000
        assert history.remaining == 34_625
        assert (history.cash_purchases, history.installment_purchases, history.credit_purchases) == (
            1, 1, 1,
        )
        assert history.first_purchase == date(2026, 1, 10)
        assert history.last_purchase == date(2026, 9, 20)
        assert history.last_payment == date(2026, 10, 2)
        assert history.overdue_installments == 3
        assert history.payment_profile == "facilities"


def test_payments_are_newest_first_with_sale_details(memory_engine: Engine) -> None:
    """Payments sort by date then id, descending, and name their sale."""
    with session_scope(memory_engine) as session:
        customer = _mixed_customer(session)
        history = customers.customer_history(session, customer.id, today=TODAY)

        assert [(item.amount, item.payment_date) for item in history.payments] == [
            (3_000, date(2026, 10, 2)),
            (5_000, date(2026, 10, 2)),
            (4_000, date(2026, 8, 3)),
            (8_875, date(2026, 7, 2)),
        ]
        newest_credit, older_credit, partial, first = history.payments
        assert newest_credit.payment_id > older_credit.payment_id
        assert newest_credit.sale_type == "credit" and newest_credit.installment_index is None
        assert newest_credit.product == "لوحة"
        assert partial.installment_index == 2 and partial.method == "BaridiMob"
        assert partial.note == "دفعة جزئية" and partial.product == "هاتف A15"
        assert first.installment_index == 1 and first.method == "CCP"
        assert first.sale_id == history.purchases[1].sale_id


def test_grace_days_setting_changes_overdue_count(memory_engine: Engine) -> None:
    """The configured grace period decides which unpaid months are overdue."""
    with session_scope(memory_engine) as session:
        customer = _mixed_customer(session)
        settings.set_value(session, "grace_days", 20)
        history = customers.customer_history(session, customer.id, today=TODAY)
        # #4 is due 1 October: with 20 grace days it is not late on 15 October.
        assert history.overdue_installments == 2
        assert history.purchases[1].status == Status.FAILED


def test_pending_then_paid_status_for_current_month(memory_engine: Engine) -> None:
    """A sale is PENDING within the grace period and PAID once settled."""
    with session_scope(memory_engine) as session:
        category = _category(session)
        customer = customers.create_customer(session, full_name="منير", category_id=category.id)
        sale = sales.create_sale(
            session, customer_id=customer.id, product="هاتف", sale_type="installment",
            wholesale_price=10_000, cash_price=12_000, rate=0, months=2,
            purchase_date=date(2026, 9, 10),
        )
        early = date(2026, 10, 3)
        history = customers.customer_history(session, customer.id, today=early)
        assert history.purchases[0].status == Status.PENDING
        assert history.purchases[0].overdue_installments == 0
        assert history.last_payment is None

        first = min(sale.installments, key=lambda row: row.installment_index)
        payments.record_installment_payment(session, first.id, first.amount_due, early, "cash")
        history = customers.customer_history(session, customer.id, today=early)
        assert history.purchases[0].status == Status.PAID
        assert history.purchases[0].paid == 6_000 and history.remaining == 6_000
        assert history.last_payment == early


def test_empty_and_cash_only_profiles(memory_engine: Engine) -> None:
    """Customers without sales have empty history; cash-only ones are cash."""
    with session_scope(memory_engine) as session:
        category = _category(session)
        empty = customers.create_customer(session, full_name="جديد", category_id=category.id)
        history = customers.customer_history(session, empty.id, today=TODAY)
        assert history.purchases == () and history.payments == ()
        assert history.purchase_count == 0 and history.total_bought == 0
        assert history.first_purchase is None and history.last_purchase is None
        assert history.payment_profile == "none"

        sales.create_sale(
            session, customer_id=empty.id, product="شاحن", sale_type="cash",
            wholesale_price=500, cash_price=900, purchase_date=date(2026, 3, 1),
        )
        history = customers.customer_history(session, empty.id, today=TODAY)
        assert history.payment_profile == "cash"
        assert history.total_paid == 900 and history.remaining == 0

        with pytest.raises(ValueError, match="Customer not found"):
            customers.customer_history(session, 9999, today=TODAY)


def test_history_query_count_does_not_grow_with_sales(memory_engine: Engine) -> None:
    """Eager loading keeps the number of SELECTs constant (no N+1)."""
    with session_scope(memory_engine) as session:
        big = _mixed_customer(session)
        small = customers.create_customer(
            session, full_name="صغير", category_id=big.category_id
        )
        sales.create_sale(
            session, customer_id=small.id, product="هاتف", sale_type="installment",
            wholesale_price=1_000, cash_price=1_200, months=2, purchase_date=date(2026, 9, 1),
        )
        big_id, small_id = big.id, small.id

    def count_queries(customer_id: int) -> int:
        statements: list[str] = []

        def record(*args: object) -> None:
            statements.append(str(args[2]))

        event.listen(memory_engine, "before_cursor_execute", record)
        try:
            with session_scope(memory_engine) as session:
                customers.customer_history(session, customer_id, today=TODAY)
        finally:
            event.remove(memory_engine, "before_cursor_execute", record)
        return sum(1 for statement in statements if statement.lstrip().upper().startswith("SELECT"))

    assert count_queries(big_id) == count_queries(small_id) <= 6


def _owner_and_seller(session: Session) -> tuple[User, User]:
    """Create an owner and a seller account."""
    owner = auth.create_first_owner(
        session, username="owner", password=PASSWORD, password_confirmation=PASSWORD
    )
    seller = auth.create_seller(
        session, owner.id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    )
    return owner, seller


def _customer_count(session: Session) -> int:
    """Return the number of saved customers."""
    return session.scalar(select(func.count()).select_from(Customer)) or 0


def test_owner_creates_customer_with_installment_sale(memory_engine: Engine) -> None:
    """Both rows and the schedule are created in one call."""
    with session_scope(memory_engine) as session:
        owner, _seller = _owner_and_seller(session)
        category = _category(session)
        customer, sale = sales.create_customer_with_sale(
            session,
            owner.id,
            customer={
                "full_name": " كريم ",
                "category_id": category.id,
                "phone": "0661234567",
                "address": "أم الطيور",
                "cheques_count": 4,
            },
            sale={
                "product": "Redmi 13",
                "sale_type": "installment",
                "wholesale_price": 20_000,
                "cash_price": 24_000,
                "rate": 35,
                "down_payment": 4_400,
                "months": 6,
                "purchase_date": date(2026, 10, 1),
            },
        )
        assert customer.id is not None and customer.full_name == "كريم"
        assert customer.address == "أم الطيور" and customer.cheques_count == 4
        assert sale.customer_id == customer.id
        assert sale.total == 32_400 and sale.financed == 28_000
        assert len(sale.installments) == 6
        assert sum(row.amount_due for row in sale.installments) == 28_000


def test_seller_gets_catalog_prices(memory_engine: Engine) -> None:
    """Sellers cannot set their own prices; non-catalog products are refused."""
    with session_scope(memory_engine) as session:
        owner, seller = _owner_and_seller(session)
        category = _category(session)
        products.create_product(
            session, owner.id, name="Galaxy A15", wholesale_price=20_000, cash_price=25_000
        )
        _customer, sale = sales.create_customer_with_sale(
            session,
            seller.id,
            customer={"full_name": "زبون البائع", "category_id": category.id},
            sale={
                "product": "Galaxy A15",
                "sale_type": "cash",
                "wholesale_price": 1,
                "cash_price": 2,
                "purchase_date": date(2026, 10, 1),
            },
        )
        assert (sale.wholesale_price, sale.cash_price, sale.total) == (20_000, 25_000, 25_000)

        before = _customer_count(session)
        with pytest.raises(auth.AuthorizationError, match="catalog"):
            sales.create_customer_with_sale(
                session,
                seller.id,
                customer={"full_name": "مرفوض", "category_id": category.id},
                sale={
                    "product": "منتج غير موجود",
                    "sale_type": "cash",
                    "wholesale_price": 1,
                    "cash_price": 2,
                },
            )
        assert _customer_count(session) == before


def test_invalid_sale_leaves_no_orphan_customer(memory_engine: Engine) -> None:
    """A failing sale rolls back the new customer but not earlier work."""
    with session_scope(memory_engine) as session:
        owner, _seller = _owner_and_seller(session)
        category = _category(session)
        kept = customers.create_customer(session, full_name="موجود", category_id=category.id)
        with pytest.raises(ValueError, match="Credit sales do not have installment months"):
            sales.create_customer_with_sale(
                session,
                owner.id,
                customer={"full_name": "يتيم", "category_id": category.id, "phone": "0770000000"},
                sale={
                    "product": "هاتف",
                    "sale_type": "credit",
                    "wholesale_price": 1_000,
                    "cash_price": 1_500,
                    "months": 3,
                },
            )
        assert session.scalars(select(Customer.full_name)).all() == ["موجود"]
        assert customers.find_duplicate_phones(session, "0770000000") == []
        assert session.scalar(select(func.count()).select_from(Sale)) == 0
        kept_id = kept.id

    with session_scope(memory_engine) as session:
        assert session.scalars(select(Customer.id)).all() == [kept_id]


def test_duplicate_phone_propagates_and_can_be_confirmed(memory_engine: Engine) -> None:
    """The duplicate-phone error is unchanged; confirming saves both rows."""
    with session_scope(memory_engine) as session:
        owner, _seller = _owner_and_seller(session)
        category = _category(session)
        customers.create_customer(
            session, full_name="الأول", category_id=category.id, phone="0551112233"
        )
        values = {"full_name": "الثاني", "category_id": category.id, "phone": "0551112233"}
        sale_values = {
            "product": "هاتف", "sale_type": "cash", "wholesale_price": 100, "cash_price": 150,
        }
        with pytest.raises(customers.DuplicateCustomerPhoneError) as caught:
            sales.create_customer_with_sale(session, owner.id, customer=values, sale=sale_values)
        assert [match.full_name for match in caught.value.matches] == ["الأول"]
        assert _customer_count(session) == 1

        customer, sale = sales.create_customer_with_sale(
            session, owner.id, customer=values, sale=sale_values, allow_duplicate_phone=True
        )
        assert customer.phone == "0551112233" and sale.customer_id == customer.id
        assert _customer_count(session) == 2


def test_inactive_account_and_bad_arguments_are_refused(memory_engine: Engine) -> None:
    """Disabled users cannot record sales; customer_id cannot be smuggled in."""
    with session_scope(memory_engine) as session:
        owner, seller = _owner_and_seller(session)
        category = _category(session)
        customer_values = {"full_name": "س", "category_id": category.id}
        sale_values = {
            "product": "هاتف", "sale_type": "cash", "wholesale_price": 100, "cash_price": 150,
        }
        auth.disable_user(session, owner.id, seller.id)
        with pytest.raises(auth.AuthorizationError):
            sales.create_customer_with_sale(
                session, seller.id, customer=customer_values, sale=sale_values
            )
        with pytest.raises(ValueError, match="customer_id"):
            sales.create_customer_with_sale(
                session, owner.id, customer=customer_values,
                sale={**sale_values, "customer_id": 1},
            )
        assert _customer_count(session) == 0
