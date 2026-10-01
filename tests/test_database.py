"""Database seeding, repository operations, and delete protections."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Installment, Payment, Sale, Setting
from app.db.session import session_scope
from app.services import repositories
from app.services.seed import DEFAULT_CATEGORIES, seed_defaults


def _create_sale(session: Session, customer_id: int, product: str) -> Sale:
    """Create a representative installment sale for repository tests."""
    return repositories.create_sale(
        session,
        customer_id=customer_id,
        product=product,
        sale_type="installment",
        wholesale_price=56_500,
        cash_price=62_800,
        rate=40,
        down_payment=10_500,
        months=10,
        total=87_920,
        financed=77_420,
        monthly_amount=7_742,
        profit=31_420,
        purchase_date=date(2026, 9, 30),
        end_date=date(2027, 7, 30),
    )


def test_seed_create_two_sales_and_preserve_financial_history(memory_engine: Engine) -> None:
    """Seed defaults and enforce category/customer/sale deletion rules."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        seed_defaults(session)
        assert set(session.scalars(select(Category.name))) == set(DEFAULT_CATEGORIES)
        settings = dict(session.execute(select(Setting.key, Setting.value)).all())
        assert '"first_of_month"' in settings["due_mode"]
        assert settings["grace_days"] == "5"

        category = session.scalar(select(Category).where(Category.name == "أساتذة"))
        assert category is not None
        customer = repositories.create_customer(
            session,
            full_name="أحمد بن صالح",
            phone="0550123456",
            category_id=category.id,
        )
        first_sale = _create_sale(session, customer.id, "Galaxy A55")
        second_sale = _create_sale(session, customer.id, "Redmi Note 14")
        assert len(customer.sales) == 2

        with pytest.raises(ValueError, match="has customers"):
            repositories.delete_category(session, category.id)
        with pytest.raises(ValueError, match="has sales"):
            repositories.delete_customer(session, customer.id)

        installment = Installment(
            sale_id=first_sale.id,
            installment_index=1,
            due_date=date(2026, 10, 1),
            amount_due=7_742,
            amount_paid=2_000,
        )
        session.add(installment)
        session.flush()
        payment = Payment(
            installment_id=installment.id,
            amount=2_000,
            payment_date=date(2026, 10, 1),
            method="cash",
        )
        session.add(payment)
        session.flush()
        payment_id = payment.id

        repositories.delete_sale(session, first_sale.id)
        session.expire_all()
        assert session.get(Sale, first_sale.id) is None
        assert session.get(Payment, payment_id) is None
        assert session.get(Sale, second_sale.id) is not None


def test_unused_category_can_be_deleted(memory_engine: Engine) -> None:
    """Allow removal of a category when it has no customers."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        category = repositories.create_category(session, "تجريبي")
        repositories.delete_category(session, category.id)
        assert session.get(Category, category.id) is None


def test_seeding_does_not_restore_owner_renamed_category(memory_engine: Engine) -> None:
    """Seed defaults only for a new database, preserving later owner edits."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        category = session.scalar(select(Category).where(Category.name == "أساتذة"))
        assert category is not None
        category.name = "معلمون"
        session.flush()

        seed_defaults(session)

        assert session.scalar(select(Category).where(Category.name == "أساتذة")) is None
        assert session.scalar(select(Category).where(Category.name == "معلمون")) is not None
