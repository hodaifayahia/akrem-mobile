"""Customer reminder links (WhatsApp) shared by every screen that offers them.

``weekly_targets`` lists, one row per client, everyone with an installment
due within the week (and the late ones) for the "remind everyone" list.
WhatsApp only lets an app open a drafted message, so the operator presses
"send" in WhatsApp for each client; every opened reminder is logged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AuditLog
from app.services.alerts import CollectionAlert, collection_alerts

ALGERIA_COUNTRY_CODE = "213"


def international_number(phone: str | None) -> str | None:
    """Convert a local Algerian mobile number to WhatsApp's digits-only format.

    ``0661 23 45 67`` becomes ``213661234567``. Numbers already in
    international form are kept; anything too short to dial returns ``None``.
    """
    if not phone:
        return None
    digits = "".join(str(int(char)) for char in phone if char.isdecimal())
    if phone.strip().startswith("+"):
        return digits if len(digits) >= 9 else None
    if digits.startswith("00"):
        digits = digits[2:]
    elif len(digits) == 10 and digits.startswith("0"):
        digits = ALGERIA_COUNTRY_CODE + digits[1:]
    return digits if len(digits) >= 9 else None


def whatsapp_link(phone: str | None, message: str) -> str | None:
    """Return a ``wa.me`` link with a drafted message, or ``None`` without a phone."""
    number = international_number(phone)
    if number is None:
        return None
    return f"https://wa.me/{number}?text={quote(message)}"


# ------------------------------------------------------------------ weekly reminders
REMINDER_ACTION = "whatsapp_reminder"
WEEK_DAYS = 7


@dataclass(frozen=True, slots=True)
class ReminderTarget:
    """One client to remind: everything they owe now or within the week, in one message."""

    customer_id: int
    customer_name: str
    phone: str | None
    amount: int
    first_due: date
    late: bool
    products: tuple[str, ...]
    last_reminded: datetime | None

    @property
    def can_send(self) -> bool:
        return international_number(self.phone) is not None

    def reminded_on(self, day: date) -> bool:
        """Whether the client was already reminded on ``day`` (local date of the log entry)."""
        return self.last_reminded is not None and self.last_reminded.date() == day


def weekly_targets(
    session: Session, *, today: date, include_late: bool = True, horizon_days: int = WEEK_DAYS
) -> list[ReminderTarget]:
    """Clients with an installment due within ``horizon_days`` (and, by default, the late ones).

    One row per client, whatever the number of installments or sales: the
    amounts are added up and the earliest due date is shown. Late clients
    come first, then by due date.
    """
    grouped: dict[int, list[CollectionAlert]] = {}
    for alert in collection_alerts(session, today=today, horizon_days=horizon_days):
        late = alert.kind != "due_soon"
        if late and not include_late:
            continue
        grouped.setdefault(alert.customer_id, []).append(alert)
    last = last_reminders(session, grouped)
    targets = [
        ReminderTarget(
            customer_id=customer_id,
            customer_name=items[0].customer_name,
            phone=items[0].customer_phone,
            amount=sum(item.amount for item in items),
            first_due=min(item.due_date for item in items),
            late=any(item.kind != "due_soon" for item in items),
            products=tuple(dict.fromkeys(item.product for item in items)),
            last_reminded=last.get(customer_id),
        )
        for customer_id, items in grouped.items()
    ]
    targets.sort(key=lambda target: (not target.late, target.first_due, target.customer_name))
    return targets


def message_for(target: ReminderTarget) -> str:
    """The drafted WhatsApp text, in the active language."""
    from app.i18n import ar

    template = ar.REMIND_LATE_MESSAGE if target.late else ar.PAYT_REMINDER
    return template.format(
        customer=target.customer_name,
        amount=f"{target.amount:,}",
        due=target.first_due.strftime("%d/%m/%Y"),
    )


def link_for(target: ReminderTarget) -> str | None:
    """``wa.me`` link with the drafted message, or None without a usable phone."""
    return whatsapp_link(target.phone, message_for(target))


def record_reminder(session: Session, user_id: int | None, customer_id: int, *, amount: int) -> None:
    """Log that a reminder was opened for this client (shown as "last reminder")."""
    session.add(AuditLog(
        user_id=user_id, action=REMINDER_ACTION, entity="customer", entity_id=customer_id,
        details=json.dumps({"amount": amount}),
    ))
    session.flush()


def last_reminders(session: Session, customer_ids: Iterable[int]) -> dict[int, datetime]:
    """``{customer id: time of the newest reminder}`` for the given clients."""
    ids = list(customer_ids)
    if not ids:
        return {}
    rows = session.execute(
        select(AuditLog.entity_id, func.max(AuditLog.created_at))
        .where(AuditLog.action == REMINDER_ACTION, AuditLog.entity == "customer", AuditLog.entity_id.in_(ids))
        .group_by(AuditLog.entity_id)
    )
    return {customer_id: _local(when) for customer_id, when in rows if when is not None}


def _local(value: datetime) -> datetime:
    """Database timestamps are UTC; compare reminder days in the shop's local time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone()
