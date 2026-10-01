"""Read services for customer rows and category tabs."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.db.models import Category, Customer, Installment, Payment
from app.db.session import session_scope
from app.services import categories, customers, sales
from app.services.status import Status


def _category(session: Session, name: str = "الفئة الأولى") -> Category:
    """Create a test category."""
    category = Category(name=name)
    session.add(category)
    session.flush()
    return category


def test_category_queries_include_counts_and_preserve_delete_protection(
    memory_engine: Engine,
) -> None:
    """Category services count customers and reuse repository write rules."""
    with session_scope(memory_engine) as session:
        first = categories.create_category(session, " أجهزة ")
        empty = categories.create_category(session, "بدون زبائن")
        customer = customers.create_customer(
            session,
            full_name="عميل",
            category_id=first.id,
            phone="0550000001",
        )

        assert [(item.name, count) for item, count in categories.list_categories(session)] == [
            ("أجهزة", 1),
            ("بدون زبائن", 0),
        ]
        renamed = categories.rename_category(session, empty.id, "اسم جديد")
        assert renamed.name == "اسم جديد"
        with pytest.raises(ValueError, match="has customers"):
            categories.delete_category(session, first.id)
        categories.delete_category(session, empty.id)
        with pytest.raises(ValueError, match="not found"):
            categories.delete_category(session, empty.id)
        assert session.get(Customer, customer.id) is not None


def test_customer_list_search_status_and_financial_summary(memory_engine: Engine) -> None:
    """Arabic-aware search returns eagerly summarized sale and status data."""
    with session_scope(memory_engine) as session:
        category = _category(session, "أجهزة")
        other_category = _category(session, "أخرى")
        customer = customers.create_customer(
            session,
            full_name="أحمد فاطمة",
            category_id=category.id,
            phone="0550123456",
        )
        installment_sale = sales.create_sale(
            session,
            customer_id=customer.id,
            product="هاتف أزرق",
            sale_type="installment",
            wholesale_price=25_000,
            cash_price=30_000,
            months=3,
            purchase_date=date(2026, 9, 30),
        )
        credit_sale = sales.create_sale(
            session,
            customer_id=customer.id,
            product="حاسوب محمول",
            sale_type="credit",
            wholesale_price=15_000,
            cash_price=20_000,
            down_payment=5_000,
            purchase_date=date(2026, 10, 2),
            expected_pay_date=date(2026, 10, 31),
        )
        first_installment = session.scalar(
            select(Installment).where(
                Installment.sale_id == installment_sale.id,
                Installment.installment_index == 1,
            )
        )
        assert first_installment is not None
        first_installment.amount_paid = 2_000
        session.add_all(
            (
                Payment(
                    installment_id=first_installment.id,
                    amount=2_000,
                    payment_date=date(2026, 10, 2),
                    method="cash",
                ),
                Payment(
                    sale_id=credit_sale.id,
                    amount=3_000,
                    payment_date=date(2026, 10, 3),
                    method="cash",
                ),
            )
        )
        another = customers.create_customer(
            session,
            full_name="سميرة",
            category_id=other_category.id,
            phone="0660000002",
        )

        rows = customers.list_customers(
            session,
            year=2026,
            month=10,
            today=date(2026, 10, 7),
            search="احمد فاطمه",
        )
        assert len(rows) == 1
        summary = rows[0]
        assert (summary.id, summary.full_name, summary.phone) == (
            customer.id,
            "أحمد فاطمة",
            "0550123456",
        )
        assert summary.category_id == category.id
        assert summary.category_name == "أجهزة"
        assert summary.products == ("هاتف أزرق", "حاسوب محمول")
        assert summary.purchase_dates == (date(2026, 9, 30), date(2026, 10, 2))
        assert summary.purchase_date == date(2026, 10, 2)
        assert summary.end_dates == (date(2026, 12, 30), None)
        assert summary.end_date == date(2026, 12, 30)
        assert summary.monthly_amount == 10_000
        assert summary.remaining_balance == 40_000
        assert summary.status is Status.FAILED
        assert summary.customer.id == customer.id
        assert len(summary.sales) == 2

        assert customers.list_customers(
            session,
            year=2026,
            month=10,
            today=date(2026, 10, 7),
            search="٠٥٥٠١٢٣٤٥٦",
        )[0].id == customer.id
        assert customers.list_customers(
            session,
            year=2026,
            month=10,
            today=date(2026, 10, 7),
            category_id=other_category.id,
        )[0].id == another.id
        assert customers.list_customers(
            session,
            year=2026,
            month=10,
            today=date(2026, 10, 7),
            status="failed",
        )[0].id == customer.id
        assert [row.id for row in customers.list_customers(
            session,
            year=2026,
            month=10,
            today=date(2026, 10, 7),
            status=Status.NONE,
        )] == [another.id]


def test_arabic_search_normalization_handles_hamza_taa_marbuta_and_diacritics() -> None:
    """Canonical search text equates the common Arabic spelling differences."""
    assert customers.normalize_search_text("إِمْرَأَةٌ") == "امراه"
    assert customers.normalize_search_text("أحمد") == customers.normalize_search_text("احمد")


def test_customer_details_eagerly_loads_relationships_and_reports_missing_id(
    memory_engine: Engine,
) -> None:
    """Detail reads include related payment history without lazy queries."""
    with session_scope(memory_engine) as session:
        category = _category(session)
        customer = customers.create_customer(
            session,
            full_name="تفاصيل العميل",
            category_id=category.id,
            phone="0550000003",
        )
        sale = sales.create_sale(
            session,
            customer_id=customer.id,
            product="هاتف",
            sale_type="installment",
            wholesale_price=20_000,
            cash_price=24_000,
            months=2,
            purchase_date=date(2026, 9, 1),
        )
        installment = session.scalar(
            select(Installment).where(Installment.sale_id == sale.id)
        )
        assert installment is not None
        session.add(
            Payment(
                installment_id=installment.id,
                amount=1_000,
                payment_date=date(2026, 10, 1),
                method="cash",
            )
        )
        session.flush()

        details = customers.get_customer_details(session, customer.id)
        assert details.id == customer.id
        assert "sales" not in inspect(details).unloaded
        assert "category" not in inspect(details).unloaded
        assert "installments" not in inspect(details.sales[0]).unloaded
        assert "credit_payments" not in inspect(details.sales[0]).unloaded
        assert "payments" not in inspect(details.sales[0].installments[0]).unloaded
        assert len(details.sales[0].installments[0].payments) == 1

        with pytest.raises(ValueError, match="Customer not found"):
            customers.get_customer_details(session, customer.id + 100)
