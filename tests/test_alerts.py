"""Collection alerts service and the grace-days setting."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Customer, Installment, Payment, Sale, Setting
from app.db.session import session_scope
from app.services import repositories, settings
from app.services.alerts import AlertSummary, CollectionAlert, collection_alerts, summarize
from app.services.seed import seed_defaults

TODAY = date(2026, 10, 10)


@pytest.fixture
def session(memory_engine: Engine) -> Iterator[Session]:
    """Seeded session (grace_days = 5)."""
    with session_scope(memory_engine) as scoped:
        seed_defaults(scoped)
        yield scoped


def _customer(session: Session, name: str, phone: str | None = "0550000001") -> Customer:
    category = session.scalar(select(Category).where(Category.name == "أساتذة"))
    assert category is not None
    return repositories.create_customer(session, full_name=name, category_id=category.id, phone=phone)


def _sale(
    session: Session,
    customer: Customer,
    *,
    sale_type: str = "installment",
    financed: int = 30_000,
    expected_pay_date: date | None = None,
    product: str = "Phone",
) -> Sale:
    return repositories.create_sale(
        session,
        customer_id=customer.id,
        product=product,
        sale_type=sale_type,
        wholesale_price=20_000,
        cash_price=financed,
        rate=0,
        down_payment=0,
        months=3 if sale_type == "installment" else None,
        total=financed,
        financed=0 if sale_type == "cash" else financed,
        monthly_amount=None,
        profit=financed - 20_000,
        purchase_date=date(2026, 6, 1),
        expected_pay_date=expected_pay_date,
    )


def _installment(
    session: Session,
    sale: Sale,
    due: date,
    *,
    amount_due: int = 10_000,
    amount_paid: int = 0,
    index: int | None = None,
) -> Installment:
    row = Installment(
        sale_id=sale.id,
        installment_index=index or len(sale.installments) + 1,
        due_date=due,
        amount_due=amount_due,
        amount_paid=amount_paid,
    )
    session.add(row)
    session.flush()
    session.refresh(sale)
    return row


def _alerts(session: Session, **kwargs: object) -> list[CollectionAlert]:
    session.expire_all()
    return collection_alerts(session, today=TODAY, **kwargs)


# --- Grace-days setting --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 5), ("3", 3), ("0", 0), ("-1", 5), ("true", 5), ('"7"', 5), ("2.5", 5), ("not json", 5)],
)
def test_get_grace_days(memory_engine: Engine, raw: str | None, expected: int) -> None:
    """Valid non-negative integers are used; anything else falls back to 5."""
    with session_scope(memory_engine) as session:
        if raw is not None:
            session.add(Setting(key="grace_days", value=raw))
            session.flush()
        assert settings.get_grace_days(session) == expected


# --- Installments --------------------------------------------------------------


def test_grace_boundary(session: Session) -> None:
    """Exactly at due + grace is still due soon; one day later is overdue."""
    customer = _customer(session, "Boundary")
    sale = _sale(session, customer)
    at_grace = _installment(session, sale, date(2026, 10, 5))  # 10/05 + 5 == today
    past_grace = _installment(session, sale, date(2026, 10, 4))

    alerts = {alert.installment_id: alert for alert in _alerts(session)}
    assert alerts[at_grace.id].kind == "due_soon"
    assert alerts[at_grace.id].days_late == 5
    assert alerts[past_grace.id].kind == "overdue"
    assert alerts[past_grace.id].days_late == 6
    assert alerts[past_grace.id].key == f"installment:{past_grace.id}:overdue"
    assert alerts[at_grace.id].key == f"installment:{at_grace.id}:due_soon"


def test_grace_days_setting_is_used(session: Session) -> None:
    """A zero grace period makes yesterday's installment overdue."""
    settings.set_value(session, "grace_days", 0)
    sale = _sale(session, _customer(session, "Zero grace"))
    yesterday = _installment(session, sale, date(2026, 10, 9))
    today_row = _installment(session, sale, TODAY)
    kinds = {alert.installment_id: alert.kind for alert in _alerts(session)}
    assert kinds == {yesterday.id: "overdue", today_row.id: "due_soon"}


def test_horizon(session: Session) -> None:
    """Due soon covers due dates up to today + horizon inclusive."""
    sale = _sale(session, _customer(session, "Horizon"))
    inside = _installment(session, sale, date(2026, 10, 17))
    outside = _installment(session, sale, date(2026, 10, 18))
    ids = {alert.installment_id for alert in _alerts(session)}
    assert inside.id in ids and outside.id not in ids
    ids = {alert.installment_id for alert in _alerts(session, horizon_days=8)}
    assert outside.id in ids
    soon = [alert for alert in _alerts(session, horizon_days=0)]
    assert soon == []
    with pytest.raises(ValueError):
        collection_alerts(session, today=TODAY, horizon_days=-1)


def test_partial_payments_and_ledger(session: Session) -> None:
    """Remaining uses the larger of the cached paid amount and the payment ledger."""
    sale = _sale(session, _customer(session, "Partial"))
    cached = _installment(session, sale, date(2026, 9, 1), amount_paid=4_000)
    ledger = _installment(session, sale, date(2026, 8, 1), amount_paid=1_000)
    session.add_all([
        Payment(installment_id=ledger.id, amount=3_000, payment_date=date(2026, 8, 2), method="cash"),
        Payment(installment_id=ledger.id, amount=2_500, payment_date=date(2026, 8, 3), method="cash"),
    ])
    stale_cache = _installment(session, sale, date(2026, 7, 1), amount_paid=0)
    session.add(Payment(installment_id=stale_cache.id, amount=10_000, payment_date=date(2026, 7, 1), method="CCP"))
    session.flush()

    amounts = {alert.installment_id: alert.amount for alert in _alerts(session)}
    assert amounts == {cached.id: 6_000, ledger.id: 4_500}


def test_fully_paid_and_cash_sales_ignored(session: Session) -> None:
    """Settled installments and cash sales produce no alerts."""
    customer = _customer(session, "Paid")
    sale = _sale(session, customer)
    _installment(session, sale, date(2026, 9, 1), amount_paid=10_000)
    _installment(session, sale, date(2026, 10, 12), amount_paid=12_000)
    _sale(session, customer, sale_type="cash")
    assert _alerts(session) == []


def test_alert_fields(session: Session) -> None:
    """Alerts carry customer and sale context."""
    customer = _customer(session, "Context", phone=None)
    sale = _sale(session, customer, product="Galaxy A55")
    row = _installment(session, sale, date(2026, 10, 15), amount_due=7_742)
    [alert] = _alerts(session)
    assert alert == CollectionAlert(
        kind="due_soon",
        customer_id=customer.id,
        customer_name="Context",
        customer_phone=None,
        sale_id=sale.id,
        installment_id=row.id,
        product="Galaxy A55",
        amount=7_742,
        due_date=date(2026, 10, 15),
        days_late=-5,
        key=f"installment:{row.id}:due_soon",
    )


# --- Credit --------------------------------------------------------------------


def test_credit_alerts(session: Session) -> None:
    """Credit is overdue only with an expected date past grace and a balance left."""
    customer = _customer(session, "Credit")
    no_date = _sale(session, customer, sale_type="credit")
    in_grace = _sale(session, customer, sale_type="credit", expected_pay_date=date(2026, 10, 5))
    late = _sale(session, customer, sale_type="credit", financed=20_000, expected_pay_date=date(2026, 10, 1))
    settled = _sale(session, customer, sale_type="credit", financed=5_000, expected_pay_date=date(2026, 9, 1))
    session.add_all([
        Payment(sale_id=late.id, amount=8_000, payment_date=date(2026, 9, 15), method="cash"),
        Payment(sale_id=settled.id, amount=5_000, payment_date=date(2026, 9, 2), method="BaridiMob"),
    ])
    session.flush()

    alerts = _alerts(session)
    assert [(alert.sale_id, alert.kind) for alert in alerts] == [(late.id, "credit_overdue")]
    [alert] = alerts
    assert alert.installment_id is None
    assert alert.amount == 12_000
    assert alert.due_date == date(2026, 10, 1)
    assert alert.days_late == 9
    assert alert.key == f"credit:{late.id}:credit_overdue"
    assert {no_date.id, in_grace.id}.isdisjoint({a.sale_id for a in alerts})


# --- Ordering and summary --------------------------------------------------------


def test_ordering(session: Session) -> None:
    """Late alerts by days late desc, then due-soon by date; ties by customer name."""
    zed = _customer(session, "Zed")
    amy = _customer(session, "Amy")
    zed_sale = _sale(session, zed)
    amy_sale = _sale(session, amy)
    zed_soon = _installment(session, zed_sale, date(2026, 10, 12))
    amy_soon = _installment(session, amy_sale, date(2026, 10, 12))
    early_soon = _installment(session, zed_sale, date(2026, 10, 8))
    zed_late = _installment(session, zed_sale, date(2026, 9, 1))
    amy_late = _installment(session, amy_sale, date(2026, 9, 1))
    oldest = _installment(session, zed_sale, date(2026, 8, 1))
    credit = _sale(session, amy, sale_type="credit", expected_pay_date=date(2026, 9, 20))

    keys = [alert.key for alert in _alerts(session)]
    assert keys == [
        f"installment:{oldest.id}:overdue",
        f"installment:{amy_late.id}:overdue",
        f"installment:{zed_late.id}:overdue",
        f"credit:{credit.id}:credit_overdue",
        f"installment:{early_soon.id}:due_soon",
        f"installment:{amy_soon.id}:due_soon",
        f"installment:{zed_soon.id}:due_soon",
    ]


def test_summarize(session: Session) -> None:
    """Summary counts and amounts per kind."""
    customer = _customer(session, "Summary")
    sale = _sale(session, customer)
    _installment(session, sale, date(2026, 9, 1), amount_due=10_000, amount_paid=2_000)
    _installment(session, sale, date(2026, 8, 1), amount_due=5_000)
    _installment(session, sale, date(2026, 10, 11), amount_due=3_000)
    _sale(session, customer, sale_type="credit", financed=9_000, expected_pay_date=date(2026, 9, 1))

    summary = summarize(_alerts(session))
    assert summary == AlertSummary(
        overdue_count=2,
        overdue_amount=13_000,
        due_soon_count=1,
        due_soon_amount=3_000,
        credit_overdue_count=1,
        credit_overdue_amount=9_000,
    )
    assert summary.total_count == 4
    assert summary.attention_count == 3
    empty = summarize([])
    assert (empty.total_count, empty.attention_count, empty.overdue_amount) == (0, 0, 0)


def test_no_n_plus_one_queries(session: Session, memory_engine: Engine) -> None:
    """The number of SELECTs does not grow with the number of sales."""
    from sqlalchemy import event

    def count_queries() -> int:
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_args) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(memory_engine, "before_cursor_execute", record)
        try:
            _alerts(session)
        finally:
            event.remove(memory_engine, "before_cursor_execute", record)
        return len(statements)

    for index in range(2):
        sale = _sale(session, _customer(session, f"Q{index}"))
        _installment(session, sale, date(2026, 9, 1))
        _sale(session, sale.customer, sale_type="credit", expected_pay_date=date(2026, 9, 1))
    few = count_queries()
    for index in range(8):
        sale = _sale(session, _customer(session, f"R{index}"))
        _installment(session, sale, date(2026, 9, 1))
        _sale(session, sale.customer, sale_type="credit", expected_pay_date=date(2026, 9, 1))
    assert count_queries() == few
