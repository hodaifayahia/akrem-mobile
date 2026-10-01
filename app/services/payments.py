"""Payment recording, balance checks, and audited payment reversals."""

from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Installment, Payment, Sale, User
from app.services import auth

_METHODS = {
    "cash": "cash",
    "ccp": "CCP",
    "baridimob": "BaridiMob",
}


def record_installment_payment_for_user(
    session: Session,
    user_id: int,
    installment_id: int,
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> Payment:
    """Record an installment payment after verifying the active operator role."""
    _require_active_operator(session, user_id)
    return record_installment_payment(
        session, installment_id, amount, payment_date, method, note
    )


def record_installment_payment(
    session: Session,
    installment_id: int,
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> Payment:
    """Record a partial or full payment against one scheduled installment."""
    _validate_amount(amount)
    clean_date = _validate_payment_date(payment_date)
    clean_method = _normalize_method(method)
    lock_payment_ledger(session)
    installment = session.scalar(
        select(Installment)
        .where(Installment.id == installment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if installment is None:
        raise ValueError("Installment not found")
    if installment.sale.sale_type != "installment":
        raise ValueError("Payments can only be recorded against installment sales")
    remaining = installment.amount_due - installment.amount_paid
    if remaining <= 0:
        raise ValueError("This installment is already fully paid")
    if amount > remaining:
        raise ValueError(f"Payment exceeds the remaining installment balance of {remaining}")

    payment = Payment(
        installment_id=installment.id,
        amount=amount,
        payment_date=clean_date,
        method=clean_method,
        note=_clean_note(note),
    )
    with session.begin_nested():
        session.add(payment)
        installment.amount_paid += amount
        installment.method = clean_method
        if installment.amount_paid >= installment.amount_due:
            installment.paid_date = clean_date
        session.flush()
    return payment


def record_credit_payment(
    session: Session,
    sale_id: int,
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> Payment:
    """Record a payment toward a credit sale's balance after down payment."""
    _validate_amount(amount)
    clean_date = _validate_payment_date(payment_date)
    clean_method = _normalize_method(method)
    lock_payment_ledger(session)
    sale = session.scalar(
        select(Sale)
        .where(Sale.id == sale_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if sale is None:
        raise ValueError("Sale not found")
    if sale.sale_type != "credit":
        raise ValueError("Credit payments can only be recorded against credit sales")
    already_paid = session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.sale_id == sale_id)
    ) or 0
    remaining = sale.financed - already_paid
    if remaining <= 0:
        raise ValueError("This credit balance is already fully paid")
    if amount > remaining:
        raise ValueError(f"Payment exceeds the remaining credit balance of {remaining}")

    payment = Payment(
        sale_id=sale.id,
        amount=amount,
        payment_date=clean_date,
        method=clean_method,
        note=_clean_note(note),
    )
    with session.begin_nested():
        session.add(payment)
        session.flush()
    return payment


def record_credit_payment_for_user(
    session: Session,
    user_id: int,
    sale_id: int,
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> Payment:
    """Record a credit payment after verifying the active operator role."""
    _require_active_operator(session, user_id)
    return record_credit_payment(
        session, sale_id, amount, payment_date, method, note
    )


def undo_last_payment(session: Session, user_id: int, reason: str) -> AuditLog:
    """Undo the most recently recorded payment, preserving an owner audit trail.

    The audit entry is flushed before the payment is deleted. Both actions and
    installment balance recalculation share a nested transaction; the caller
    remains responsible for committing the enclosing session.
    """
    owner = auth.require_owner(session, user_id)
    clean_reason = reason.strip() if isinstance(reason, str) else ""
    if not clean_reason:
        raise ValueError("A reason is required to undo a payment")
    lock_payment_ledger(session)
    payment = session.scalar(
        select(Payment)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        raise ValueError("There is no payment to undo")

    installment_id = payment.installment_id
    if installment_id is not None:
        installment = session.scalar(
            select(Installment)
            .where(Installment.id == installment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    elif payment.sale_id is not None:
        session.scalar(
            select(Sale)
            .where(Sale.id == payment.sale_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    details = {
        "payment_id": payment.id,
        "amount": payment.amount,
        "payment_date": payment.payment_date.isoformat(),
        "method": payment.method,
        "note": payment.note,
        "installment_id": payment.installment_id,
        "sale_id": payment.sale_id,
    }
    audit = AuditLog(
        user_id=owner.id,
        action="undo_payment",
        entity="payment",
        entity_id=payment.id,
        reason=clean_reason,
        details=json.dumps(details, ensure_ascii=False, sort_keys=True),
    )
    with session.begin_nested():
        session.add(audit)
        session.flush()
        session.delete(payment)
        session.flush()
        if installment_id is not None:
            if installment is not None:
                _recalculate_installment(session, installment)
        session.flush()
    return audit


def _recalculate_installment(session: Session, installment: Installment) -> None:
    """Rebuild cached installment paid fields from its surviving payment rows."""
    payments = list(
        session.scalars(
            select(Payment)
            .where(Payment.installment_id == installment.id)
            .order_by(Payment.id)
        )
    )
    installment.amount_paid = sum(payment.amount for payment in payments)
    if not payments:
        installment.paid_date = None
        installment.method = None
        return
    latest = payments[-1]
    installment.method = latest.method
    if installment.amount_paid >= installment.amount_due:
        paid_so_far = 0
        installment.paid_date = None
        for payment in payments:
            paid_so_far += payment.amount
            if paid_so_far >= installment.amount_due:
                installment.paid_date = payment.payment_date
                break
    else:
        installment.paid_date = None


def _validate_amount(amount: int) -> None:
    """Require a positive whole-dinar payment amount."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError("Payment amount must be a positive whole number")


def _validate_payment_date(value: date) -> date:
    """Require a calendar date, rejecting datetimes to avoid silent truncation."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("Payment date must be a date")
    return value


def _normalize_method(method: str) -> str:
    """Normalize a supported payment method and reject unknown values."""
    if not isinstance(method, str):
        raise ValueError("Payment method must be cash, CCP, or BaridiMob")
    canonical = _METHODS.get(method.strip().casefold())
    if canonical is None:
        raise ValueError("Payment method must be cash, CCP, or BaridiMob")
    return canonical


def _clean_note(note: str | None) -> str | None:
    """Normalize optional payment notes to trimmed text or ``None``."""
    if note is None:
        return None
    if not isinstance(note, str):
        raise ValueError("Payment note must be text")
    return note.strip() or None


def _require_active_operator(session: Session, user_id: int) -> None:
    """Require an active owner or seller account for collections."""
    user = session.get(User, user_id)
    if user is None or user.disabled or user.role not in {"owner", "seller"}:
        raise auth.AuthorizationError("An active operator account is required")


def lock_payment_ledger(session: Session) -> None:
    """Serialize payment inserts and undo selection across PostgreSQL clients."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(809775249632416)))
