"""The shop's debt book: money people owe the shop, and money the shop owes.

* ``receivable`` ("ديون لي"): someone must pay the shop back.
* ``payable`` ("ديون عليّ"): the shop must pay someone (a supplier, a lender).

Each debt belongs to a person (first and last name, e-mail, national ID card
number, phone). It is repaid in one payment (optional due date) or by
facility: ``months`` long, one payment every ``payment_interval`` months,
split exactly like an installment sale (see :mod:`app.services.calc`).
Payments are recorded one by one (partial payments are fine) and are applied
to the schedule oldest-first, which gives each debt its status.

Only the owner manages the debt book.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import Debt, DebtParty, DebtPayment
from app.services import auth, calc
from app.services.customers import normalize_phone, normalize_search_text
from app.services.settings import get_grace_days, get_value
from app.services.spreadsheet import western_digits

RECEIVABLE = "receivable"
PAYABLE = "payable"
DIRECTIONS = (RECEIVABLE, PAYABLE)
PLAN_SINGLE = "single"
PLAN_INSTALLMENTS = "installments"
METHODS = ("cash", "CCP", "BaridiMob", "bank")

STATUS_PAID = "paid"
STATUS_OVERDUE = "overdue"
STATUS_DUE_SOON = "due_soon"
STATUS_ON_TRACK = "on_track"
STATUSES = (STATUS_OVERDUE, STATUS_DUE_SOON, STATUS_ON_TRACK, STATUS_PAID)
DUE_SOON_DAYS = 7
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DebtError(ValueError):
    """A debt change was refused; ``code`` names the reason for the UI.

    Codes: ``name``, ``email``, ``national_id``, ``national_id_taken``,
    ``phone``, ``amount``, ``plan``, ``direction``, ``overpaid``,
    ``below_paid``, ``not_found``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PersonDetails:
    """Who the debt is with."""

    first_name: str
    last_name: str
    email: str | None = None
    national_id: str | None = None
    phone: str | None = None


@dataclass(frozen=True)
class DebtTerms:
    """How much, why, since when, and how it is repaid."""

    amount: int
    debt_date: date
    reason: str | None = None
    plan: str = PLAN_SINGLE
    due_date: date | None = None
    months: int | None = None
    interval: int = 1


@dataclass(frozen=True)
class ScheduleRow:
    """One expected repayment and how much of it the payments already cover."""

    index: int
    due_date: date
    amount: int
    covered: int

    @property
    def open_amount(self) -> int:
        return self.amount - self.covered


@dataclass(frozen=True)
class DebtSummary:
    """A debt as shown in the debt pages."""

    id: int
    direction: str
    party_id: int
    full_name: str
    first_name: str
    last_name: str
    email: str | None
    national_id: str | None
    phone: str | None
    reason: str | None
    debt_date: date
    amount: int
    paid: int
    remaining: int
    plan: str
    months: int | None
    interval: int
    due_date: date | None
    next_due_date: date | None
    next_due_amount: int
    overdue_amount: int
    status: str
    payment_count: int


@dataclass(frozen=True)
class DebtTotals:
    """Headline numbers of one side of the debt book."""

    count: int
    amount: int
    paid: int
    remaining: int
    overdue: int
    overdue_count: int
    due_soon_count: int


# ------------------------------------------------------------------ changes
def create_debt(
    session: Session,
    owner_user_id: int,
    *,
    direction: str,
    person: PersonDetails,
    terms: DebtTerms,
) -> Debt:
    """Record a new debt; the person is reused when the national ID (or exact name) matches."""
    auth.require_owner(session, owner_user_id)
    if direction not in DIRECTIONS:
        raise DebtError("direction", "Unknown debt direction")
    party = _find_or_create_party(session, person)
    clean = _validated_terms(terms)
    debt = Debt(party_id=party.id, direction=direction, **clean)
    session.add(debt)
    session.flush()
    return debt


def update_debt(
    session: Session,
    owner_user_id: int,
    debt_id: int,
    *,
    person: PersonDetails,
    terms: DebtTerms,
) -> Debt:
    """Change a debt and its person's details; the amount cannot go below what was already paid."""
    auth.require_owner(session, owner_user_id)
    debt = _get(session, debt_id)
    clean = _validated_terms(terms)
    if clean["amount"] < paid_amount(debt):
        raise DebtError("below_paid", "The amount is lower than what was already paid")
    _apply_person(session, debt.party, person)
    for field, value in clean.items():
        setattr(debt, field, value)
    _flush(session)
    return debt


def delete_debt(session: Session, owner_user_id: int, debt_id: int) -> None:
    """Remove a debt (entered by mistake) with its payments."""
    auth.require_owner(session, owner_user_id)
    session.delete(_get(session, debt_id))
    session.flush()


def record_payment(
    session: Session,
    owner_user_id: int,
    debt_id: int,
    *,
    amount: int,
    payment_date: date,
    method: str = "cash",
    note: str | None = None,
) -> DebtPayment:
    """Record money received (or paid out); never more than what remains."""
    auth.require_owner(session, owner_user_id)
    debt = _get(session, debt_id)
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise DebtError("amount", "Payment must be a positive whole number of dinars")
    if amount > debt.amount - paid_amount(debt):
        raise DebtError("overpaid", "Payment is more than the remaining amount")
    if method not in METHODS:
        raise DebtError("method", "Unknown payment method")
    payment = DebtPayment(
        debt_id=debt.id, amount=amount, payment_date=payment_date, method=method,
        note=(note or "").strip()[:300] or None,
    )
    session.add(payment)
    session.flush()
    session.expire(debt, ["payments"])
    return payment


def delete_payment(session: Session, owner_user_id: int, payment_id: int) -> None:
    """Undo a payment recorded by mistake."""
    auth.require_owner(session, owner_user_id)
    payment = session.get(DebtPayment, payment_id)
    if payment is None:
        raise DebtError("not_found", "Payment not found")
    debt = payment.debt
    session.delete(payment)
    session.flush()
    session.expire(debt, ["payments"])


# ------------------------------------------------------------------ queries
def schedule(debt: Debt, *, due_mode: str = "first_of_month") -> list[ScheduleRow]:
    """Expected repayments, each with how much the payments cover (oldest first)."""
    if debt.plan == PLAN_INSTALLMENTS and debt.months:
        offsets = calc.payment_offsets(debt.months, debt.payment_interval or 1)
        base, remainder = divmod(debt.amount, len(offsets))
        amounts = [base] * len(offsets)
        amounts[-1] += remainder
        dues = []
        for offset in offsets:
            target = calc.add_months(debt.debt_date, offset)
            dues.append(target.replace(day=1) if due_mode == "first_of_month" else target)
    else:
        amounts = [debt.amount]
        dues = [debt.due_date or debt.debt_date]
    left = paid_amount(debt)
    rows = []
    for index, (due, amount) in enumerate(zip(dues, amounts, strict=True), start=1):
        covered = min(amount, left)
        left -= covered
        rows.append(ScheduleRow(index, due, amount, covered))
    return rows


def paid_amount(debt: Debt) -> int:
    """Total of the payments recorded on a debt."""
    return sum(payment.amount for payment in debt.payments)


def summarize(debt: Debt, today: date, *, grace_days: int = 5, due_mode: str = "first_of_month") -> DebtSummary:
    """A debt's totals, next repayment and status as of ``today``.

    Overdue: something that fell due (plus the grace days) is still unpaid.
    A single debt without a due date is never overdue.
    """
    rows = schedule(debt, due_mode=due_mode)
    paid = paid_amount(debt)
    remaining = max(0, debt.amount - paid)
    open_rows = [row for row in rows if row.open_amount > 0]
    has_date = debt.plan == PLAN_INSTALLMENTS or debt.due_date is not None
    late_cutoff = today - timedelta(days=grace_days)
    overdue = sum(row.open_amount for row in open_rows if has_date and row.due_date < late_cutoff)
    next_row = open_rows[0] if open_rows else None
    if remaining == 0:
        status = STATUS_PAID
    elif overdue > 0:
        status = STATUS_OVERDUE
    elif has_date and next_row is not None and next_row.due_date <= today + timedelta(days=DUE_SOON_DAYS):
        status = STATUS_DUE_SOON
    else:
        status = STATUS_ON_TRACK
    party = debt.party
    return DebtSummary(
        id=debt.id,
        direction=debt.direction,
        party_id=party.id,
        full_name=party.full_name,
        first_name=party.first_name,
        last_name=party.last_name,
        email=party.email,
        national_id=party.national_id,
        phone=party.phone,
        reason=debt.reason,
        debt_date=debt.debt_date,
        amount=debt.amount,
        paid=paid,
        remaining=remaining,
        plan=debt.plan,
        months=debt.months,
        interval=debt.payment_interval or 1,
        due_date=debt.due_date,
        next_due_date=next_row.due_date if next_row is not None and has_date else None,
        next_due_amount=next_row.open_amount if next_row is not None else 0,
        overdue_amount=overdue,
        status=status,
        payment_count=len(debt.payments),
    )


def list_debts(
    session: Session,
    direction: str,
    *,
    today: date,
    status: str | None = None,
    search: str | None = None,
) -> list[DebtSummary]:
    """One side of the debt book, most urgent first (overdue, due soon, on track, paid).

    ``search`` matches the name, e-mail, national ID, phone and reason.
    """
    if direction not in DIRECTIONS:
        raise DebtError("direction", "Unknown debt direction")
    if status not in (None, "", *STATUSES):
        raise DebtError("status", f"Unknown status {status}")
    grace_days, due_mode = _settings(session)
    debts = session.scalars(
        select(Debt)
        .where(Debt.direction == direction)
        .options(selectinload(Debt.party), selectinload(Debt.payments))
    ).all()
    key = normalize_search_text(western_digits(search or ""))
    rows = []
    for debt in debts:
        summary = summarize(debt, today, grace_days=grace_days, due_mode=due_mode)
        if status and summary.status != status:
            continue
        if key and not any(
            key in normalize_search_text(western_digits(value or ""))
            for value in (summary.full_name, summary.email, summary.national_id, summary.phone, summary.reason)
        ):
            continue
        rows.append(summary)
    order = {name: position for position, name in enumerate(STATUSES)}
    rows.sort(key=lambda row: (order[row.status], row.next_due_date or date.max, row.full_name, row.id))
    return rows


def totals(session: Session, direction: str, *, today: date) -> DebtTotals:
    """Count, amounts and overdue figures for one side of the debt book."""
    rows = list_debts(session, direction, today=today)
    return DebtTotals(
        count=sum(1 for row in rows if row.remaining > 0),
        amount=sum(row.amount for row in rows),
        paid=sum(row.paid for row in rows),
        remaining=sum(row.remaining for row in rows),
        overdue=sum(row.overdue_amount for row in rows),
        overdue_count=sum(1 for row in rows if row.status == STATUS_OVERDUE),
        due_soon_count=sum(1 for row in rows if row.status == STATUS_DUE_SOON),
    )


def payments_between(session: Session, direction: str, start: date, end: date) -> int:
    """Money received (receivable) or paid out (payable) between two dates, inclusive."""
    rows = session.scalars(
        select(DebtPayment.amount)
        .join(Debt)
        .where(Debt.direction == direction, DebtPayment.payment_date >= start, DebtPayment.payment_date <= end)
    )
    return sum(rows)


def get_debt(session: Session, debt_id: int) -> Debt:
    """A debt with its person and payments loaded."""
    return _get(session, debt_id)


# ------------------------------------------------------------------ helpers
def _get(session: Session, debt_id: int) -> Debt:
    debt = session.get(Debt, debt_id)
    if debt is None:
        raise DebtError("not_found", "Debt not found")
    return debt


def _settings(session: Session) -> tuple[int, str]:
    due_mode = get_value(session, "due_mode", "first_of_month")
    return get_grace_days(session), due_mode if due_mode in ("first_of_month", "purchase_day") else "first_of_month"


def clean_person(person: PersonDetails) -> dict[str, Any]:
    """Validated person fields (names required; e-mail, ID card number and phone checked)."""
    first = " ".join((person.first_name or "").split())[:80]
    last = " ".join((person.last_name or "").split())[:80]
    if not first or not last:
        raise DebtError("name", "First and last name are required")
    email = (person.email or "").strip() or None
    if email is not None and (len(email) > 120 or not _EMAIL.match(email)):
        raise DebtError("email", "Invalid e-mail address")
    national_id = re.sub(r"[\s\-]", "", western_digits(person.national_id or "")) or None
    if national_id is not None and (not national_id.isdigit() or not 6 <= len(national_id) <= 30):
        raise DebtError("national_id", "The national ID card number must contain only digits")
    try:
        phone = normalize_phone(person.phone) if (person.phone or "").strip() else None
    except ValueError as error:
        raise DebtError("phone", "Invalid phone number") from error
    return {"first_name": first, "last_name": last, "email": email, "national_id": national_id, "phone": phone}


def _find_or_create_party(session: Session, person: PersonDetails) -> DebtParty:
    fields = clean_person(person)
    party = None
    if fields["national_id"]:
        party = session.scalar(select(DebtParty).where(DebtParty.national_id == fields["national_id"]))
    if party is None and not fields["national_id"]:
        key = (normalize_search_text(fields["first_name"]), normalize_search_text(fields["last_name"]))
        party = next(
            (
                candidate for candidate in session.scalars(select(DebtParty).where(DebtParty.national_id.is_(None)))
                if (normalize_search_text(candidate.first_name), normalize_search_text(candidate.last_name)) == key
            ),
            None,
        )
    if party is None:
        party = DebtParty(**fields)
        session.add(party)
        _flush(session)
        return party
    _apply_person(session, party, person)
    return party


def _apply_person(session: Session, party: DebtParty, person: PersonDetails) -> None:
    fields = clean_person(person)
    for field, value in fields.items():
        if value is not None or field in ("first_name", "last_name"):
            setattr(party, field, value)
    _flush(session)


def _validated_terms(terms: DebtTerms) -> dict[str, Any]:
    if isinstance(terms.amount, bool) or not isinstance(terms.amount, int) or terms.amount <= 0:
        raise DebtError("amount", "The amount must be a positive whole number of dinars")
    if not isinstance(terms.debt_date, date):
        raise DebtError("plan", "The debt date is required")
    if terms.plan == PLAN_INSTALLMENTS:
        try:
            calc.validate_plan(terms.months, terms.interval)
        except ValueError as error:
            raise DebtError("plan", str(error)) from error
        months, interval, due_date = terms.months, terms.interval, None
    elif terms.plan == PLAN_SINGLE:
        months, interval, due_date = None, 1, terms.due_date
        if due_date is not None and due_date < terms.debt_date:
            raise DebtError("plan", "The due date is before the debt date")
    else:
        raise DebtError("plan", "Unknown repayment plan")
    return {
        "amount": terms.amount,
        "debt_date": terms.debt_date,
        "reason": " ".join((terms.reason or "").split())[:200] or None,
        "plan": terms.plan,
        "due_date": due_date,
        "months": months,
        "payment_interval": interval,
    }


def _flush(session: Session) -> None:
    try:
        session.flush()
    except IntegrityError as error:
        raise DebtError("national_id_taken", "Another person already has this national ID number") from error
