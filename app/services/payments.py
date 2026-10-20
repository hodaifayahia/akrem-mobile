"""Payment recording, balance checks, and audited payment reversals."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

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


def installment_sale_remaining(session: Session, sale_id: int) -> int:
    """What a client still owes on all the installments of one sale."""
    rows = session.scalars(select(Installment).where(Installment.sale_id == sale_id))
    return sum(max(0, row.amount_due - row.amount_paid) for row in rows)


def record_sale_payment(
    session: Session,
    sale_id: int,
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> list[Payment]:
    """Record any amount a client pays on an installment sale, oldest installment first.

    Clients do not always pay the scheduled amount: they may pay 20,000 one
    month, nothing the next and 10,000 after that. The amount may be smaller
    or larger than one installment, up to the sale's whole remaining
    balance. It fills the unpaid installments in order; each installment it
    touches gets its own payment row, and the rows share a ``batch_id`` so
    the amount is undone as one payment.
    """
    _validate_amount(amount)
    clean_date = _validate_payment_date(payment_date)
    clean_method = _normalize_method(method)
    clean_note = _clean_note(note)
    lock_payment_ledger(session)
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise ValueError("Sale not found")
    if sale.sale_type != "installment":
        raise ValueError("Payments can only be recorded against installment sales")
    rows = list(
        session.scalars(
            select(Installment)
            .where(Installment.sale_id == sale_id)
            .order_by(Installment.installment_index)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    remaining = sum(max(0, row.amount_due - row.amount_paid) for row in rows)
    if remaining <= 0:
        raise ValueError("This sale is already fully paid")
    if amount > remaining:
        raise ValueError(f"Payment exceeds the remaining sale balance of {remaining}")

    batch_id = uuid.uuid4().hex
    created: list[Payment] = []
    left = amount
    with session.begin_nested():
        for row in rows:
            if left <= 0:
                break
            open_amount = row.amount_due - row.amount_paid
            if open_amount <= 0:
                continue
            part = min(left, open_amount)
            payment = Payment(
                installment_id=row.id,
                amount=part,
                payment_date=clean_date,
                method=clean_method,
                note=clean_note,
                batch_id=batch_id,
            )
            session.add(payment)
            row.amount_paid += part
            row.method = clean_method
            if row.amount_paid >= row.amount_due:
                row.paid_date = clean_date
            created.append(payment)
            left -= part
        session.flush()
    return created


def record_sale_payment_for_user(
    session: Session,
    user_id: int,
    sale_id: int,
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> list[Payment]:
    """Record a free-amount installment payment after verifying the active operator role."""
    _require_active_operator(session, user_id)
    return record_sale_payment(session, sale_id, amount, payment_date, method, note)


def record_sales_payment_for_user(
    session: Session,
    user_id: int,
    sale_ids: Iterable[int],
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> list[Payment]:
    """Record one payment across several matching installment sale lines.

    Every created row shares a batch id, so the amount can be undone as one
    payment even when a duplicate product was stored on separate sale lines.
    """
    _require_active_operator(session, user_id)
    ids = list(dict.fromkeys(int(value) for value in sale_ids))
    if not ids:
        raise ValueError("At least one sale is required")
    _validate_amount(amount)
    clean_date = _validate_payment_date(payment_date)
    clean_method = _normalize_method(method)
    clean_note = _clean_note(note)
    lock_payment_ledger(session)
    sales = list(session.scalars(
        select(Sale)
        .where(Sale.id.in_(ids))
        .order_by(Sale.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))
    if len(sales) != len(ids) or any(sale.sale_type != "installment" for sale in sales):
        raise ValueError("Payments can only be recorded against the selected installment sales")
    if len({sale.customer_id for sale in sales}) != 1:
        raise ValueError("The selected sales must belong to one customer")
    rows = list(session.scalars(
        select(Installment)
        .where(Installment.sale_id.in_(ids))
        .options(selectinload(Installment.payments))
        .order_by(Installment.installment_index, Installment.sale_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))
    balances = {
        row.id: max(row.amount_paid, sum(payment.amount for payment in row.payments))
        for row in rows
    }
    remaining = sum(max(0, row.amount_due - balances[row.id]) for row in rows)
    if amount > remaining:
        raise ValueError(f"Payment exceeds the remaining sale balance of {remaining}")

    batch_id = uuid.uuid4().hex
    created: list[Payment] = []
    left = amount
    with session.begin_nested():
        for row in rows:
            if left <= 0:
                break
            open_amount = row.amount_due - balances[row.id]
            if open_amount <= 0:
                continue
            part = min(left, open_amount)
            payment = Payment(
                installment_id=row.id,
                amount=part,
                payment_date=clean_date,
                method=clean_method,
                note=clean_note,
                batch_id=batch_id,
            )
            session.add(payment)
            created.append(payment)
            row.amount_paid = balances[row.id] + part
            row.method = clean_method
            if row.amount_paid >= row.amount_due:
                row.paid_date = clean_date
            left -= part
        session.flush()
    return created


def record_credit_sales_payment_for_user(
    session: Session,
    user_id: int,
    sale_ids: Iterable[int],
    amount: int,
    payment_date: date,
    method: str,
    note: str | None = None,
) -> list[Payment]:
    """Record one payment across matching credit sale lines as one undoable batch."""
    _require_active_operator(session, user_id)
    ids = list(dict.fromkeys(int(value) for value in sale_ids))
    if not ids:
        raise ValueError("At least one sale is required")
    _validate_amount(amount)
    clean_date = _validate_payment_date(payment_date)
    clean_method = _normalize_method(method)
    clean_note = _clean_note(note)
    lock_payment_ledger(session)
    sales = list(session.scalars(
        select(Sale)
        .where(Sale.id.in_(ids))
        .options(selectinload(Sale.credit_payments))
        .order_by(Sale.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))
    if len(sales) != len(ids) or any(sale.sale_type != "credit" for sale in sales):
        raise ValueError("Payments can only be recorded against the selected credit sales")
    if len({sale.customer_id for sale in sales}) != 1:
        raise ValueError("The selected sales must belong to one customer")
    balances = {
        sale.id: max(
            0,
            sale.financed - sum(payment.amount for payment in sale.credit_payments),
        )
        for sale in sales
    }
    remaining = sum(balances.values())
    if amount > remaining:
        raise ValueError(f"Payment exceeds the remaining credit balance of {remaining}")

    batch_id = uuid.uuid4().hex
    created: list[Payment] = []
    left = amount
    with session.begin_nested():
        for sale in sales:
            if left <= 0:
                break
            part = min(left, balances[sale.id])
            if part <= 0:
                continue
            payment = Payment(
                sale_id=sale.id,
                amount=part,
                payment_date=clean_date,
                method=clean_method,
                note=clean_note,
                batch_id=batch_id,
            )
            session.add(payment)
            created.append(payment)
            left -= part
        session.flush()
    return created


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
    clean_reason = _required_reason(reason)
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
    return _remove_payment_batch(session, owner, payment, clean_reason)


def reverse_payment(session: Session, user_id: int, payment_id: int, reason: str) -> AuditLog:
    """Return one amount a client paid (any one, not only the latest); owner only.

    The whole amount received is removed, even when it was spread over
    several installments or basket products, so the balance it paid is owed
    again. A reason is required and the reversal is written to the audit log
    (``undo_payment``), like "undo last payment".
    """
    owner = auth.require_owner(session, user_id)
    clean_reason = _required_reason(reason)
    lock_payment_ledger(session)
    payment = session.scalar(
        select(Payment)
        .where(Payment.id == payment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        raise ValueError("Payment not found")
    return _remove_payment_batch(session, owner, payment, clean_reason)


def _required_reason(reason: str) -> str:
    clean_reason = reason.strip() if isinstance(reason, str) else ""
    if not clean_reason:
        raise ValueError("A reason is required to undo a payment")
    return clean_reason


def _remove_payment_batch(session: Session, owner: User, payment: Payment, clean_reason: str) -> AuditLog:
    """Delete a payment with the rest of its batch, rebuild the installments, and audit it."""
    # An amount spread over several installments is undone as a whole.
    group = [payment]
    if payment.batch_id is not None:
        group = list(
            session.scalars(
                select(Payment)
                .where(Payment.batch_id == payment.batch_id)
                .order_by(Payment.id)
                .with_for_update()
            )
        )
    installment_ids = [part.installment_id for part in group if part.installment_id is not None]
    installments = list(
        session.scalars(
            select(Installment)
            .where(Installment.id.in_(installment_ids))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ) if installment_ids else []
    if payment.installment_id is None and payment.sale_id is not None:
        session.scalar(
            select(Sale)
            .where(Sale.id == payment.sale_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    details = {
        "payment_id": payment.id,
        "amount": sum(part.amount for part in group),
        "payment_date": payment.payment_date.isoformat(),
        "method": payment.method,
        "note": payment.note,
        "installment_id": payment.installment_id,
        "sale_id": payment.sale_id,
    }
    if len(group) > 1:
        details["parts"] = [
            {"payment_id": part.id, "installment_id": part.installment_id, "amount": part.amount}
            for part in group
        ]
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
        for part in group:
            session.delete(part)
        session.flush()
        for installment in installments:
            _recalculate_installment(session, installment)
        session.flush()
    return audit


def delete_payments(
    session: Session,
    owner_user_id: int,
    *,
    installment_ids: Iterable[int] = (),
    credit_sale_ids: Iterable[int] = (),
) -> int:
    """Delete every payment recorded on some installments and credit sales.

    The installments become unpaid again (their paid amount, date and method
    are rebuilt), each removed payment is kept in the audit log, and the
    number of payments removed is returned. A credit down payment belongs to
    the sale and stays.
    """
    owner = auth.require_owner(session, owner_user_id)
    installments = list(dict.fromkeys(int(value) for value in installment_ids))
    credit_sales = list(dict.fromkeys(int(value) for value in credit_sale_ids))
    targets = []
    if installments:
        targets.append(Payment.installment_id.in_(installments))
    if credit_sales:
        targets.append(Payment.sale_id.in_(credit_sales))
    if not targets:
        return 0
    lock_payment_ledger(session)
    removed = session.scalars(select(Payment).where(or_(*targets)).order_by(Payment.id)).all()
    for payment in removed:
        session.add(AuditLog(
            user_id=owner.id,
            action="delete_payment",
            entity="payment",
            entity_id=payment.id,
            details=json.dumps({
                "amount": payment.amount,
                "payment_date": payment.payment_date.isoformat(),
                "method": payment.method,
                "note": payment.note,
                "installment_id": payment.installment_id,
                "sale_id": payment.sale_id,
            }, ensure_ascii=False, sort_keys=True),
        ))
        session.delete(payment)
    session.flush()
    if installments:
        for installment in session.scalars(select(Installment).where(Installment.id.in_(installments))):
            _recalculate_installment(session, installment)
        session.flush()
    return len(removed)


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
