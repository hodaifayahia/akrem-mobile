"""Installment and credit payment workflows and owner-only undo behavior."""

import json
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Category, Installment, Payment, User
from app.db.session import session_scope
from app.services import auth, customers, payments, sales
from app.services.seed import seed_defaults


def _customer(session: Session):
    """Create a customer with a seeded category."""
    category = session.scalar(select(Category).order_by(Category.id))
    assert category is not None
    return customers.create_customer(
        session,
        full_name="Payment Test Customer",
        category_id=category.id,
    )


def _installment(session: Session) -> Installment:
    """Create a one-month installment plan for a known outstanding balance."""
    customer = _customer(session)
    sale = sales.create_sale(
        session,
        customer_id=customer.id,
        product="Payment test phone",
        sale_type="installment",
        wholesale_price=800,
        cash_price=1_000,
        months=1,
        purchase_date=date(2026, 9, 30),
    )
    installment = session.scalar(
        select(Installment).where(Installment.sale_id == sale.id)
    )
    assert installment is not None
    return installment


def _credit_sale(session: Session):
    """Create a credit sale with a 900-dinar balance after its down payment."""
    customer = _customer(session)
    return sales.create_sale(
        session,
        customer_id=customer.id,
        product="Credit test phone",
        sale_type="credit",
        wholesale_price=800,
        cash_price=1_000,
        down_payment=100,
        purchase_date=date(2026, 9, 30),
    )


def _owner_and_seller(session: Session) -> tuple[User, User]:
    """Create active owner and seller accounts for authorization tests."""
    owner = auth.create_first_owner(
        session,
        username="owner",
        password="owner-pass-1",
        password_confirmation="owner-pass-1",
    )
    seller = User(
        username="seller",
        password_hash=auth.hash_password("seller-pass-1"),
        role="seller",
    )
    session.add(seller)
    session.flush()
    return owner, seller


def test_installment_partial_full_payment_and_overpayment(memory_engine: Engine) -> None:
    """Update paid fields for partial/full payments and reject overpayment."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        installment = _installment(session)

        partial = payments.record_installment_payment(
            session,
            installment.id,
            300,
            date(2026, 10, 3),
            "cash",
            "  first part  ",
        )
        assert (partial.amount, partial.method, partial.note) == (300, "cash", "first part")
        assert (installment.amount_paid, installment.paid_date, installment.method) == (
            300,
            None,
            "cash",
        )
        with pytest.raises(ValueError, match="exceeds the remaining"):
            payments.record_installment_payment(
                session, installment.id, 701, date(2026, 10, 4), "CCP"
            )

        final = payments.record_installment_payment(
            session, installment.id, 700, date(2026, 10, 5), "baridimob"
        )
        assert final.method == "BaridiMob"
        assert (installment.amount_paid, installment.paid_date, installment.method) == (
            1_000,
            date(2026, 10, 5),
            "BaridiMob",
        )
        with pytest.raises(ValueError, match="already fully paid"):
            payments.record_installment_payment(
                session, installment.id, 1, date(2026, 10, 6), "cash"
            )
        assert session.scalar(select(func.count()).select_from(Payment)) == 2


def test_reject_invalid_payment_amount_method_and_target(memory_engine: Engine) -> None:
    """Reject invalid money, method, dates, and missing installment IDs."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        installment = _installment(session)
        for invalid_amount in (0, -1, True, 1.5):
            with pytest.raises(ValueError, match="positive whole number"):
                payments.record_installment_payment(
                    session,
                    installment.id,
                    invalid_amount,  # type: ignore[arg-type]
                    date(2026, 10, 1),
                    "cash",
                )
        with pytest.raises(ValueError, match="method must be"):
            payments.record_installment_payment(
                session, installment.id, 1, date(2026, 10, 1), "Cheque"
            )
        with pytest.raises(ValueError, match="Installment not found"):
            payments.record_installment_payment(
                session, -1, 1, date(2026, 10, 1), "cash"
            )
        with pytest.raises(ValueError, match="date"):
            payments.record_installment_payment(
                session,
                installment.id,
                1,
                date(2026, 10, 1).isoformat(),  # type: ignore[arg-type]
                "cash",
            )
        assert session.scalar(select(func.count()).select_from(Payment)) == 0


def test_credit_balance_payments_and_overpayment(memory_engine: Engine) -> None:
    """Credit payments reduce the balance after down payment without installments."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        sale = _credit_sale(session)
        first = payments.record_credit_payment(
            session, sale.id, 400, date(2026, 10, 2), "CCP"
        )
        assert (first.sale_id, first.installment_id, first.amount) == (sale.id, None, 400)
        with pytest.raises(ValueError, match="exceeds the remaining"):
            payments.record_credit_payment(
                session, sale.id, 501, date(2026, 10, 3), "cash"
            )
        payments.record_credit_payment(
            session, sale.id, 500, date(2026, 10, 4), "BaridiMob"
        )
        with pytest.raises(ValueError, match="already fully paid"):
            payments.record_credit_payment(
                session, sale.id, 1, date(2026, 10, 5), "cash"
            )
        assert session.scalar(select(func.count()).select_from(Installment)) == 0
        assert session.scalar(select(func.sum(Payment.amount))) == 900


def test_undo_is_owner_only_audited_and_recalculates_paid_fields(memory_engine: Engine) -> None:
    """Require owner role, log the reason, and restore the cached installment state."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        owner, seller = _owner_and_seller(session)
        installment = _installment(session)
        first = payments.record_installment_payment(
            session, installment.id, 600, date(2026, 10, 2), "cash"
        )
        final = payments.record_installment_payment(
            session, installment.id, 400, date(2026, 10, 5), "CCP"
        )
        assert installment.paid_date == date(2026, 10, 5)
        assert installment.method == "CCP"

        with pytest.raises(auth.AuthorizationError):
            payments.undo_last_payment(session, seller.id, "seller cannot undo")
        assert session.get(Payment, final.id) is not None
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 0

        audit = payments.undo_last_payment(session, owner.id, "wrong amount entered")
        assert audit.user_id == owner.id
        assert audit.action == "undo_payment"
        assert audit.entity == "payment"
        assert audit.entity_id == final.id
        assert audit.reason == "wrong amount entered"
        assert json.loads(audit.details or "{}") == {
            "amount": 400,
            "installment_id": installment.id,
            "method": "CCP",
            "note": None,
            "payment_date": "2026-10-05",
            "payment_id": final.id,
            "sale_id": None,
        }
        assert session.get(Payment, final.id) is None
        assert (installment.amount_paid, installment.paid_date, installment.method) == (
            600,
            None,
            "cash",
        )

        payments.undo_last_payment(session, owner.id, "remove remaining test payment")
        assert session.get(Payment, first.id) is None
        assert (installment.amount_paid, installment.paid_date, installment.method) == (
            0,
            None,
            None,
        )
        assert session.scalar(select(func.count()).select_from(AuditLog)) == 2


def test_undo_credit_payment_and_require_reason_or_existing_payment(memory_engine: Engine) -> None:
    """Audit credit payment reversals and validate reason/payment availability."""
    with session_scope(memory_engine) as session:
        seed_defaults(session)
        owner, _seller = _owner_and_seller(session)
        sale = _credit_sale(session)
        payment = payments.record_credit_payment(
            session, sale.id, 250, date(2026, 10, 2), "cash"
        )
        with pytest.raises(ValueError, match="reason is required"):
            payments.undo_last_payment(session, owner.id, "  ")
        audit = payments.undo_last_payment(session, owner.id, "recorded on wrong sale")
        assert audit.entity_id == payment.id
        assert session.get(Payment, payment.id) is None
        assert audit.reason == "recorded on wrong sale"
        with pytest.raises(ValueError, match="no payment to undo"):
            payments.undo_last_payment(session, owner.id, "nothing to undo")
