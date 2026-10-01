"""Collection alerts for the notification center and native OS notifications.

Alerts follow the same rules as monthly status evaluation: an unpaid
installment is late only after its due date plus the configured grace days.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import Installment, Sale
from app.services.settings import get_grace_days

AlertKind = Literal["overdue", "due_soon", "credit_overdue"]
_LATE_KINDS: frozenset[str] = frozenset({"overdue", "credit_overdue"})


@dataclass(frozen=True, slots=True)
class CollectionAlert:
    """One amount the shop should collect, with enough context to display it."""

    kind: AlertKind
    customer_id: int
    customer_name: str
    customer_phone: str | None
    sale_id: int
    installment_id: int | None
    product: str
    amount: int
    due_date: date
    days_late: int
    key: str


@dataclass(frozen=True, slots=True)
class AlertSummary:
    """Counts and unpaid totals per alert kind."""

    overdue_count: int
    overdue_amount: int
    due_soon_count: int
    due_soon_amount: int
    credit_overdue_count: int
    credit_overdue_amount: int

    @property
    def total_count(self) -> int:
        """Number of alerts of every kind."""
        return self.overdue_count + self.due_soon_count + self.credit_overdue_count

    @property
    def attention_count(self) -> int:
        """Number of late alerts (overdue installments and overdue credit)."""
        return self.overdue_count + self.credit_overdue_count


def collection_alerts(
    session: Session, *, today: date, horizon_days: int = 7
) -> list[CollectionAlert]:
    """Return sorted overdue, due-soon, and overdue-credit alerts as of ``today``.

    Late alerts come first (most days late first), then upcoming ones by due date.
    """
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 0:
        raise ValueError("horizon_days must be a non-negative integer")
    grace_days = get_grace_days(session)
    alerts = _installment_alerts(session, today, horizon_days, grace_days)
    alerts.extend(_credit_alerts(session, today, grace_days))
    return sorted(alerts, key=_sort_key)


def summarize(alerts: Iterable[CollectionAlert]) -> AlertSummary:
    """Count alerts and add up their unpaid amounts per kind."""
    counts = {"overdue": 0, "due_soon": 0, "credit_overdue": 0}
    amounts = {"overdue": 0, "due_soon": 0, "credit_overdue": 0}
    for alert in alerts:
        counts[alert.kind] += 1
        amounts[alert.kind] += alert.amount
    return AlertSummary(
        overdue_count=counts["overdue"],
        overdue_amount=amounts["overdue"],
        due_soon_count=counts["due_soon"],
        due_soon_amount=amounts["due_soon"],
        credit_overdue_count=counts["credit_overdue"],
        credit_overdue_amount=amounts["credit_overdue"],
    )


def _installment_alerts(
    session: Session, today: date, horizon_days: int, grace_days: int
) -> list[CollectionAlert]:
    """Build alerts for unpaid installments that are late or due within the horizon."""
    rows = session.scalars(
        select(Installment)
        .join(Installment.sale)
        .where(
            Sale.sale_type == "installment",
            Installment.due_date <= today + timedelta(days=horizon_days),
            # A cached paid amount at or above the due amount is always settled.
            Installment.amount_paid < Installment.amount_due,
        )
        .options(
            selectinload(Installment.payments),
            joinedload(Installment.sale).joinedload(Sale.customer),
        )
    ).all()
    alerts: list[CollectionAlert] = []
    for installment in rows:
        remaining = installment_remaining(installment)
        if remaining <= 0:
            continue
        late = today > installment.due_date + timedelta(days=grace_days)
        kind: AlertKind = "overdue" if late else "due_soon"
        alerts.append(_alert(
            kind,
            installment.sale,
            installment_id=installment.id,
            amount=remaining,
            due_date=installment.due_date,
            today=today,
            key=f"installment:{installment.id}:{kind}",
        ))
    return alerts


def _credit_alerts(session: Session, today: date, grace_days: int) -> list[CollectionAlert]:
    """Build alerts for credit balances still open after the expected date plus grace."""
    sales = session.scalars(
        select(Sale)
        .where(
            Sale.sale_type == "credit",
            Sale.expected_pay_date.is_not(None),
            Sale.expected_pay_date < today - timedelta(days=grace_days),
        )
        .options(selectinload(Sale.credit_payments), joinedload(Sale.customer))
    ).all()
    alerts: list[CollectionAlert] = []
    for sale in sales:
        remaining = credit_remaining(sale)
        if remaining <= 0 or sale.expected_pay_date is None:
            continue
        alerts.append(_alert(
            "credit_overdue",
            sale,
            installment_id=None,
            amount=remaining,
            due_date=sale.expected_pay_date,
            today=today,
            key=f"credit:{sale.id}:credit_overdue",
        ))
    return alerts


def installment_remaining(installment: Installment) -> int:
    """Unpaid dinars on an installment, trusting the larger of cache and ledger."""
    ledger = sum(payment.amount for payment in installment.payments)
    return installment.amount_due - max(installment.amount_paid, ledger)


def credit_remaining(sale: Sale) -> int:
    """Unpaid dinars on a credit sale after its down payment and credit payments."""
    return sale.financed - sum(payment.amount for payment in sale.credit_payments)


def _alert(
    kind: AlertKind,
    sale: Sale,
    *,
    installment_id: int | None,
    amount: int,
    due_date: date,
    today: date,
    key: str,
) -> CollectionAlert:
    """Create one alert with customer context taken from the sale."""
    customer = sale.customer
    return CollectionAlert(
        kind=kind,
        customer_id=customer.id,
        customer_name=customer.full_name,
        customer_phone=customer.phone,
        sale_id=sale.id,
        installment_id=installment_id,
        product=sale.product,
        amount=amount,
        due_date=due_date,
        days_late=(today - due_date).days,
        key=key,
    )


def _sort_key(alert: CollectionAlert) -> tuple[int, int, str, int, str]:
    """Late alerts by days late (desc), then upcoming by due date (asc)."""
    if alert.kind in _LATE_KINDS:
        return (0, -alert.days_late, alert.customer_name, alert.customer_id, alert.key)
    return (1, alert.due_date.toordinal(), alert.customer_name, alert.customer_id, alert.key)
