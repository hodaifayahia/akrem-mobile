"""Postpone an installment the client cannot pay on time (تأجيل الدفعة).

When a client says they will not pay this month, the owner or a seller
chooses one of three ways:

* ``days``: the installment falls due a few days later (1 to 31 days, e.g. 3,
  5 or 10); nothing else changes.
* ``next_month``: what is still unpaid on it moves onto the next installment,
  so next time the client pays both parts together. The postponed installment
  is settled at what was already paid on it (0 if nothing). Not possible on
  the last installment.
* ``extra_month``: the plan gets one more month (5 months become 6): this
  installment and every later one fall due one month later, amounts unchanged.

The products of a basket share one schedule, so they are postponed together
(pass every sale id of the basket). Each postponement writes a note on the
installments it changed and an ``installment_postponed`` audit-log entry.
"""

from __future__ import annotations

import json
from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AuditLog, Installment, Sale, User
from app.i18n import ar
from app.services import auth
from app.services.calc import MAX_PLAN_MONTHS, add_months
from app.services.payments import lock_payment_ledger

KIND_DAYS = "days"
KIND_NEXT_MONTH = "next_month"
KIND_EXTRA_MONTH = "extra_month"
KINDS = (KIND_DAYS, KIND_NEXT_MONTH, KIND_EXTRA_MONTH)
MAX_DAYS = 31


class PostponeError(ValueError):
    """A postponement that cannot be made; ``code`` tells the UI which message to show.

    Codes: ``kind`` (unknown way), ``days`` (days out of range), ``sales``
    (not installment sales of one client), ``paid`` (nothing left to pay on
    that installment), ``last`` (no next installment to move it to) and
    ``too_long`` (the plan would exceed the longest plan).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PostponeResult:
    """What a postponement changed."""

    kind: str
    installment_index: int
    sale_ids: tuple[int, ...]
    amount: int
    due_dates: tuple[date, ...]


def open_amount(installment: Installment) -> int:
    """What is still unpaid on a loaded installment (payment rows or a marked amount)."""
    paid = max(installment.amount_paid, sum(payment.amount for payment in installment.payments))
    return max(0, installment.amount_due - paid)


def can_move_to_next(sales: Iterable[Sale], installment_index: int) -> bool:
    """Whether every loaded sale has an installment after ``installment_index``."""
    return all(
        any(row.installment_index == installment_index + 1 for row in sale.installments)
        for sale in sales
    )


def postpone_installment(
    session: Session,
    user_id: int,
    sale_ids: Iterable[int],
    installment_index: int,
    kind: str,
    *,
    days: int | None = None,
    reason: str | None = None,
) -> PostponeResult:
    """Postpone installment ``installment_index`` of the given sales (one sale, or a basket)."""
    _require_operator(session, user_id)
    if kind not in KINDS:
        raise PostponeError("kind", f"Unknown postponement: {kind}")
    if kind == KIND_DAYS and (
        isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS
    ):
        raise PostponeError("days", f"Days must be between 1 and {MAX_DAYS}")
    ids = list(dict.fromkeys(int(value) for value in sale_ids))
    lock_payment_ledger(session)
    sales = list(session.scalars(
        select(Sale)
        .where(Sale.id.in_(ids))
        .options(selectinload(Sale.installments).selectinload(Installment.payments))
        .order_by(Sale.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))
    if (
        not ids
        or len(sales) != len(ids)
        or any(sale.sale_type != "installment" for sale in sales)
        or len({sale.customer_id for sale in sales}) != 1
    ):
        raise PostponeError("sales", "Only installment sales of one client can be postponed")
    targets = [
        (sale, row)
        for sale in sales
        for row in sale.installments
        if row.installment_index == installment_index and open_amount(row) > 0
    ]
    if not targets:
        raise PostponeError("paid", "Nothing is left to pay on this installment")

    moved = sum(open_amount(row) for _sale, row in targets)
    before = {row.id: row.due_date for _sale, row in targets}
    with session.begin_nested():
        if kind == KIND_DAYS:
            for _sale, row in targets:
                row.due_date = row.due_date + timedelta(days=days or 0)
                _add_note(row, ar.POSTPONE_NOTE_DAYS.format(days=days, date=_shown(before[row.id])))
        elif kind == KIND_NEXT_MONTH:
            _move_to_next(targets, installment_index)
        else:
            _add_month(sales, targets, installment_index)
        session.add(AuditLog(
            user_id=user_id,
            action="installment_postponed",
            entity="sale",
            entity_id=sales[0].id,
            reason=(reason or "").strip() or None,
            details=json.dumps({
                "kind": kind,
                "days": days if kind == KIND_DAYS else None,
                "installment_index": installment_index,
                "sale_ids": [sale.id for sale in sales],
                "amount": moved,
                "due_dates": {
                    str(row.id): [before[row.id].isoformat(), row.due_date.isoformat()]
                    for _sale, row in targets
                },
            }, ensure_ascii=False, sort_keys=True),
        ))
        session.flush()
    return PostponeResult(
        kind=kind,
        installment_index=installment_index,
        sale_ids=tuple(sale.id for sale in sales),
        amount=moved,
        due_dates=tuple(sorted({row.due_date for _sale, row in targets})),
    )


def _move_to_next(targets: list[tuple[Sale, Installment]], installment_index: int) -> None:
    """Add what is left of each installment to the sale's next one."""
    pairs: list[tuple[Installment, Installment]] = []
    for sale, row in targets:
        following = next(
            (item for item in sale.installments if item.installment_index == installment_index + 1), None
        )
        if following is None:
            raise PostponeError("last", "The last installment has no next installment")
        pairs.append((row, following))
    for row, following in pairs:
        left = open_amount(row)
        row.amount_due -= left
        following.amount_due += left
        _add_note(row, ar.POSTPONE_NOTE_MOVED.format(amount=f"{left:,}", n=following.installment_index))
        _add_note(following, ar.POSTPONE_NOTE_RECEIVED.format(amount=f"{left:,}", n=row.installment_index))


def _add_month(sales: list[Sale], targets: list[tuple[Sale, Installment]], installment_index: int) -> None:
    """Give each postponed sale one more month: this installment and the later ones move a month."""
    postponed = {sale.id for sale, _row in targets}
    for sale in sales:
        if sale.id in postponed and (sale.months or 0) + 1 > MAX_PLAN_MONTHS:
            raise PostponeError("too_long", f"A plan cannot be longer than {MAX_PLAN_MONTHS} months")
    for sale in sales:
        if sale.id not in postponed:
            continue
        for row in sale.installments:
            if row.installment_index >= installment_index:
                row.due_date = next_month(row.due_date, sale.purchase_date)
        sale.months = (sale.months or 0) + 1
        sale.end_date = add_months(sale.purchase_date, sale.months)
    for _sale, row in targets:
        _add_note(row, ar.POSTPONE_NOTE_MONTH)


def next_month(due: date, purchase_date: date) -> date:
    """``due`` one month later, keeping the purchase day an earlier month had to shorten (31 → 30 → 31)."""
    target = add_months(due, 1)
    day = due.day
    if due.day == monthrange(due.year, due.month)[1] and purchase_date.day > due.day:
        day = purchase_date.day
    return target.replace(day=min(day, monthrange(target.year, target.month)[1]))


def _add_note(row: Installment, text: str) -> None:
    """Append a line to an installment's note."""
    row.note = f"{row.note}\n{text}" if row.note else text


def _shown(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _require_operator(session: Session, user_id: int) -> None:
    """The owner or an active seller may postpone, as they record payments."""
    user = session.get(User, user_id)
    if user is None or user.disabled or user.role not in {"owner", "seller"}:
        raise auth.AuthorizationError("An active operator account is required")
