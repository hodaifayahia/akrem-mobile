"""Owner requests of 2026-10-06: one history row per amount paid, postponing an installment
(تأجيل الدفعة), the business IDs at the foot of the commitment form, and the round logo."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Category, Customer, Sale
from app.db.session import session_scope
from app.i18n import ar
from app.services import (
    auth, customers, payments, pdf_forms, postponement, print_templates, products, sales, stock,
)
from app.services.seed import seed_defaults
from app.services.status import Status, for_month
from app.ui.widgets.customer_history import transaction_label

PASSWORD = "StrongPass123"
BOUGHT = date(2026, 10, 3)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(memory_engine: Engine) -> Iterator[Session]:
    with session_scope(memory_engine) as scoped:
        seed_defaults(scoped)
        yield scoped


@pytest.fixture
def owner_id(session: Session) -> int:
    return auth.create_first_owner(
        session, username="owner", password=PASSWORD, password_confirmation=PASSWORD
    ).id


def _client(session: Session) -> int:
    category = session.scalar(select(Category).limit(1))
    return customers.create_customer(session, full_name="زبون", category_id=category.id).id


def _line(name: str, cash: int = 30000, months: int = 6) -> dict:
    """An installment line at 0 % so the amounts are easy to follow (30 000 / 6 = 5 000 a month)."""
    return {
        "product": name, "sale_type": "installment", "wholesale_price": cash * 4 // 5, "cash_price": cash,
        "rate": 0, "down_payment": 0, "months": months, "purchase_date": BOUGHT,
    }


def _sales(session: Session, owner_id: int, *lines: dict) -> tuple[int, list[Sale]]:
    """One client buying ``lines`` together; each product gets a phone in stock first (every sale takes one)."""
    for line in lines:
        product, _created = products.find_or_create_product(
            session, owner_id, str(line["product"]),
            cash_price=int(line["cash_price"]), wholesale_price=int(line["wholesale_price"]),
        )
        stock.add_units(session, owner_id, product_id=product.id)
    client = _client(session)
    return client, sales.create_sales_batch(session, owner_id, customer_id=client, lines=list(lines))


def _rows(session: Session, sale: Sale) -> list:
    session.refresh(sale)
    return sorted(sale.installments, key=lambda row: row.installment_index)


# ------------------------------------------------- one row per amount paid
def test_a_payment_over_several_installments_is_one_history_row(session: Session, owner_id: int) -> None:
    client, (phone,) = _sales(session, owner_id, _line("A16"))
    payments.record_sales_payment_for_user(session, owner_id, [phone.id], 12_000, BOUGHT, "cash")

    history = customers.customer_history(session, client, today=BOUGHT)
    (row,) = history.purchases[0].transactions

    assert (row.amount, row.remaining_after) == (12_000, 18_000)
    assert row.installment_indexes == (1, 2, 3)
    assert transaction_label(row) == ar.HIST_TX_INSTALLMENTS.format(n="، ".join("123"))


def test_a_basket_payment_shows_exactly_the_amount_typed(session: Session, owner_id: int) -> None:
    client, phones = _sales(session, owner_id, _line("X70", 60000), _line("Honor x50", 30000))
    payments.record_sales_payment_for_user(session, owner_id, [p.id for p in phones], 30_000, BOUGHT, "cash")
    payments.record_sales_payment_for_user(session, owner_id, [p.id for p in phones], 3_000, BOUGHT, "cash")

    purchase = customers.customer_history(session, client, today=BOUGHT).purchases[0]

    # 30 000 then 3 000: two rows, whatever the products and installments it was spread over.
    assert [row.amount for row in purchase.transactions] == [30_000, 3_000]
    assert [row.remaining_after for row in purchase.transactions] == [60_000, 57_000]
    assert purchase.transactions[0].product == "X70 + Honor x50"


def test_the_receipt_of_a_spread_payment_is_for_the_whole_amount(
    session: Session, owner_id: int, qapp: QApplication, tmp_path: Path
) -> None:
    client, phones = _sales(session, owner_id, _line("X70", 60000), _line("Honor x50", 30000))
    parts = payments.record_sales_payment_for_user(session, owner_id, [p.id for p in phones], 30_000, BOUGHT, "cash")
    for phone in phones:
        session.refresh(phone)
    payment = parts[-1]
    sale = next(phone for phone in phones if any(row.id == payment.installment_id for row in phone.installments))

    details = pdf_forms._batch_receipt_details(tuple(phones), sale, payment)
    assert details["amount"] == 30_000
    assert details["sale_balance_after"] == 60_000
    assert details["installment_numbers"] == (1, 2)

    output = pdf_forms.generate_payment_receipt_pdf(
        tmp_path / "receipt.pdf", session.get(Customer, client), sale, payment, basket=phones,
    )
    assert output.stat().st_size > 0


# ------------------------------------------------------------ postponement
def test_postpone_a_few_days_moves_only_the_due_date(session: Session, owner_id: int) -> None:
    _client_id, (phone,) = _sales(session, owner_id, _line("A16"))
    first_due = _rows(session, phone)[0].due_date

    result = postponement.postpone_installment(
        session, owner_id, [phone.id], 1, postponement.KIND_DAYS, days=10, reason="راتب متأخر"
    )

    rows = _rows(session, phone)
    assert rows[0].due_date == date(2026, 11, 13) == result.due_dates[0]
    assert first_due == date(2026, 11, 3)
    assert [row.amount_due for row in rows] == [5_000] * 6
    assert rows[1].due_date == date(2026, 12, 3)
    assert "10" in rows[0].note
    # Late payment is judged from the new date: on 10/11 + 5 days of grace it is not failed.
    assert for_month(rows, 2026, 11, date(2026, 11, 12), grace_days=5) == Status.PENDING
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "installment_postponed"))
    assert audit.reason == "راتب متأخر"
    assert json.loads(audit.details)["days"] == 10


def test_postpone_to_next_month_pays_two_parts_together(session: Session, owner_id: int) -> None:
    _client_id, (phone,) = _sales(session, owner_id, _line("A16"))
    payments.record_sales_payment_for_user(session, owner_id, [phone.id], 1_000, BOUGHT, "cash")

    result = postponement.postpone_installment(session, owner_id, [phone.id], 1, postponement.KIND_NEXT_MONTH)

    rows = _rows(session, phone)
    assert result.amount == 4_000
    # Installment 1 is settled at what was paid; installment 2 carries the rest.
    assert [row.amount_due for row in rows[:3]] == [1_000, 9_000, 5_000]
    assert sum(row.amount_due for row in rows) == 30_000
    assert for_month(rows, 2026, 11, date(2026, 11, 20), grace_days=5) == Status.PAID


def test_postpone_one_more_month_lengthens_the_plan(session: Session, owner_id: int) -> None:
    _client_id, (phone,) = _sales(session, owner_id, _line("A16", months=5))
    payments.record_sales_payment_for_user(session, owner_id, [phone.id], 6_000, BOUGHT, "cash")

    postponement.postpone_installment(session, owner_id, [phone.id], 2, postponement.KIND_EXTRA_MONTH)

    rows = _rows(session, phone)
    assert phone.months == 6
    assert phone.end_date == date(2027, 4, 3)
    assert [row.due_date for row in rows] == [
        date(2026, 11, 3), date(2027, 1, 3), date(2027, 2, 3), date(2027, 3, 3), date(2027, 4, 3),
    ]
    assert [row.amount_due for row in rows] == [6_000] * 5


def test_a_basket_is_postponed_together(session: Session, owner_id: int) -> None:
    _client_id, phones = _sales(session, owner_id, _line("X70", 60000), _line("Honor x50", 30000))

    result = postponement.postpone_installment(
        session, owner_id, [p.id for p in phones], 1, postponement.KIND_NEXT_MONTH
    )

    assert result.amount == 15_000
    assert [[row.amount_due for row in _rows(session, phone)][:2] for phone in phones] == [
        [0, 20_000], [0, 10_000],
    ]


def test_what_cannot_be_postponed(session: Session, owner_id: int) -> None:
    _client_id, (phone,) = _sales(session, owner_id, _line("A16", months=2))
    with pytest.raises(postponement.PostponeError) as last:
        postponement.postpone_installment(session, owner_id, [phone.id], 2, postponement.KIND_NEXT_MONTH)
    assert last.value.code == "last"
    with pytest.raises(postponement.PostponeError) as days:
        postponement.postpone_installment(session, owner_id, [phone.id], 1, postponement.KIND_DAYS, days=0)
    assert days.value.code == "days"

    payments.record_sales_payment_for_user(session, owner_id, [phone.id], 15_000, BOUGHT, "cash")
    with pytest.raises(postponement.PostponeError) as paid:
        postponement.postpone_installment(session, owner_id, [phone.id], 1, postponement.KIND_DAYS, days=3)
    assert paid.value.code == "paid"


def test_next_month_keeps_the_purchase_day() -> None:
    bought = date(2026, 1, 31)
    assert postponement.next_month(date(2026, 2, 28), bought) == date(2026, 3, 31)
    assert postponement.next_month(date(2026, 3, 31), bought) == date(2026, 4, 30)
    assert postponement.next_month(date(2026, 3, 1), date(2026, 1, 15)) == date(2026, 4, 1)


# ------------------------------------------------- business IDs and the logo
def test_the_commitment_ends_with_rc_nis_and_nif_without_ai(session: Session, owner_id: int, qapp: QApplication) -> None:
    client, (phone,) = _sales(session, owner_id, _line("A16"))
    templates = print_templates.save_business_ids(session, owner_id, nis="0013163", nif="0013160")
    templates = print_templates.save(session, owner_id, {**print_templates.to_json(templates), "business_rc": "13B0986"})

    html = pdf_forms.commitment_html(session.get(Customer, client), phone, templates, pdf_forms._logo_uri(None))
    footer = html[html.index("RC : "):]

    for text in ("RC : 13B0986", "NIS : 0013163", "NIF : 0013160"):
        assert text in footer
    assert footer.index("RC") < footer.index("NIS") < footer.index("NIF")
    assert "AI :" not in html  # the AI number was removed (owner's choice 2026-10-18)


def test_settings_and_the_print_page_share_the_nis_and_nif(session: Session, owner_id: int) -> None:
    print_templates.save(session, owner_id, print_templates.default_templates())  # page saved, IDs blank
    print_templates.save_business_ids(session, owner_id, nis="111", nif="222")

    loaded = print_templates.load(session)
    assert (loaded.business_nis, loaded.business_nif, loaded.business_rc) == ("111", "222", "")


def test_documents_show_the_logo_in_a_circle(qapp: QApplication) -> None:
    logo = QImage(QUrl(pdf_forms._logo_uri(None)).toLocalFile())
    assert not logo.isNull() and logo.width() == logo.height()
    assert logo.pixelColor(0, 0).alpha() == 0  # the square's corner is cut away
    assert logo.pixelColor(logo.width() // 2, logo.height() // 2).alpha() == 255
