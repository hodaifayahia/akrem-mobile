"""Owner requests of 2026-10-18: every sale (credit too) and every basket line leaves the stock,
the payment method of a purchase can be changed, and any payment can be returned."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Category, Installment, Payment, Sale, User
from app.db.session import dispose_database_engines, session_scope
from app.services import auth, categories, customers, payments, products, sales, stock
from app.services.seed import seed_defaults

PASSWORD = "StrongPass123"
DAY = date(2026, 9, 1)


# ================================================================== services
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


def _customer(session: Session) -> int:
    category = session.scalar(select(Category).limit(1))
    return customers.create_customer(session, full_name="زبون", category_id=category.id).id


def _product(session: Session, owner_id: int, name: str, units: int, cash: int = 30000) -> int:
    product = products.create_product(session, owner_id, name=name, cash_price=cash, wholesale_price=cash * 4 // 5)
    stock.add_units(session, owner_id, product_id=product.id, quantity=units)
    return product.id


def _available(session: Session, product_id: int) -> int:
    return len(stock.available_units(session, product_id))


def _line(name: str, sale_type: str, cash: int = 30000, **extra: object) -> dict:
    values = {
        "product": name, "sale_type": sale_type, "wholesale_price": cash * 4 // 5, "cash_price": cash,
        "rate": 35 if sale_type == "installment" else 0, "down_payment": 0,
        "months": 6 if sale_type == "installment" else None, "purchase_date": DAY,
    }
    values.update(extra)
    return values


def _paid_after_purchase(session: Session, sale_ids: list[int]) -> list[tuple[date, int]]:
    """Every amount received after the purchase (one per batch), oldest first."""
    rows = session.scalars(
        select(Payment).outerjoin(Installment)
        .where((Payment.sale_id.in_(sale_ids)) | (Installment.sale_id.in_(sale_ids)))
        .order_by(Payment.payment_date, Payment.id)
    ).all()
    batches: dict[object, list[Payment]] = {}
    for row in rows:
        batches.setdefault(row.batch_id or row.id, []).append(row)
    return [(parts[0].payment_date, sum(part.amount for part in parts)) for parts in batches.values()]


# ------------------------------------------------------------------ stock
def test_a_basket_of_credit_and_installment_lines_with_typed_imeis_takes_every_phone(
    session: Session, owner_id: int
) -> None:
    redmi = _product(session, owner_id, "Redmi 13", 3)
    iphone = _product(session, owner_id, "13 PRO", 2, cash=90000)

    saved = sales.create_sales_batch(session, owner_id, customer_id=_customer(session), lines=[
        _line("Redmi 13", "credit", imei="356789012345678"),
        _line("13 PRO", "credit", cash=90000, imei="356789012345679"),
    ])

    assert (_available(session, redmi), _available(session, iphone)) == (2, 1)
    assert [unit.imei for sale in saved for unit in sale.stock_items] == ["356789012345678", "356789012345679"]


def test_a_typed_quantity_with_an_imei_takes_that_many_phones(session: Session, owner_id: int) -> None:
    redmi = _product(session, owner_id, "Redmi 13", 3)
    sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="installment",
        wholesale_price=48000, cash_price=60000, rate=35, months=6, quantity=2, imei="356789012345678",
    )
    assert _available(session, redmi) == 1


# ------------------------------------------------------------------ payment method
def test_credit_with_payments_becomes_installment_keeping_every_amount(session: Session, owner_id: int) -> None:
    _product(session, owner_id, "Redmi 13", 1)
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="credit",
        wholesale_price=24000, cash_price=30000, down_payment=5000, purchase_date=DAY,
    )
    payments.record_credit_payment(session, sale.id, 7000, date(2026, 9, 20), "cash")

    sales.change_sale_type(session, owner_id, [sale.id], "installment", rate=35, months=5)

    session.refresh(sale)
    assert (sale.sale_type, sale.total, sale.down_payment, sale.financed) == ("installment", 40500, 5000, 35500)
    assert sale.credit_payments == []
    rows = sorted(sale.installments, key=lambda row: row.installment_index)
    assert len(rows) == 5 and sum(row.amount_due for row in rows) == 35500
    assert (rows[0].amount_due, rows[0].amount_paid) == (7100, 7000)  # 100 still owed on the first one
    assert _paid_after_purchase(session, [sale.id]) == [(date(2026, 9, 20), 7000)]
    assert customers.customer_history(session, sale.customer_id, today=DAY).remaining == 40500 - 12000
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "sale_type_changed"))
    assert audit is not None and audit.user_id == owner_id


def test_installment_with_payments_becomes_credit(session: Session, owner_id: int) -> None:
    _product(session, owner_id, "Redmi 13", 1)
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="installment",
        wholesale_price=24000, cash_price=30000, rate=35, months=6, purchase_date=DAY,
    )
    payments.record_sale_payment(session, sale.id, 9000, date(2026, 10, 1), "ccp")  # spread on 2 installments

    sales.change_sale_type(session, owner_id, [sale.id], "credit", expected_pay_date=date(2026, 12, 1))

    session.refresh(sale)
    assert (sale.sale_type, sale.total, sale.rate, sale.months) == ("credit", 30000, 0, None)
    assert sale.installments == [] and sale.expected_pay_date == date(2026, 12, 1)
    assert [(payment.amount, payment.method) for payment in sale.credit_payments] == [(9000, "CCP")]


def test_credit_becomes_cash_fully_paid(session: Session, owner_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="credit",
        wholesale_price=24000, cash_price=30000, down_payment=10000, purchase_date=DAY,
    )
    payments.record_credit_payment(session, sale.id, 5000, date(2026, 9, 10), "cash")

    sales.change_sale_type(session, owner_id, [sale.id], "cash")

    session.refresh(sale)
    assert (sale.sale_type, sale.total, sale.down_payment, sale.financed) == ("cash", 30000, 0, 30000)
    assert sale.credit_payments == []
    assert customers.customer_history(session, sale.customer_id, today=DAY).remaining == 0


def test_cash_becomes_credit_with_a_typed_down_payment(session: Session, owner_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="cash",
        wholesale_price=24000, cash_price=30000, purchase_date=DAY,
    )
    sales.change_sale_type(session, owner_id, [sale.id], "credit", down_payment=12000)
    session.refresh(sale)
    assert (sale.sale_type, sale.down_payment, sale.financed) == ("credit", 12000, 18000)


def test_a_basket_changes_together_with_one_percentage(session: Session, owner_id: int) -> None:
    _product(session, owner_id, "Redmi 13", 1)
    _product(session, owner_id, "Case", 1, cash=3000)
    saved = sales.create_sales_batch(session, owner_id, customer_id=_customer(session), lines=[
        _line("Redmi 13", "credit", cash=30000, down_payment=10000),
        _line("Case", "credit", cash=3000),
    ])
    ids = [sale.id for sale in saved]

    sales.change_sale_type(session, owner_id, ids, "installment", rate=35, months=6)

    for sale in saved:
        session.refresh(sale)
    assert {sale.sale_type for sale in saved} == {"installment"}
    assert sum(sale.total for sale in saved) == 33000 * 135 // 100
    assert sum(sale.down_payment for sale in saved) == 10000


def test_received_money_above_the_new_total_is_refused(session: Session, owner_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="installment",
        wholesale_price=24000, cash_price=30000, rate=35, months=6, down_payment=5000, purchase_date=DAY,
    )
    payments.record_sale_payment(session, sale.id, 30000, date(2026, 10, 1), "cash")
    with pytest.raises(ValueError, match="more than the new total"):
        sales.change_sale_type(session, owner_id, [sale.id], "credit")


def test_only_the_owner_changes_the_payment_method(session: Session, owner_id: int) -> None:
    seller = auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    )
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="cash",
        wholesale_price=24000, cash_price=30000, purchase_date=DAY,
    )
    with pytest.raises(auth.AuthorizationError):
        sales.change_sale_type(session, seller.id, [sale.id], "credit")


# ------------------------------------------------------------------ returning a payment
def test_any_payment_can_be_returned_not_only_the_last(session: Session, owner_id: int) -> None:
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="installment",
        wholesale_price=24000, cash_price=30000, rate=35, months=6, purchase_date=DAY,
    )
    first = payments.record_sale_payment(session, sale.id, 9000, date(2026, 10, 1), "cash")  # 2 installments
    payments.record_sale_payment(session, sale.id, 3000, date(2026, 11, 1), "cash")

    audit = payments.reverse_payment(session, owner_id, first[0].id, "خطأ في التسجيل")

    assert audit.action == "undo_payment" and audit.reason == "خطأ في التسجيل"
    assert _paid_after_purchase(session, [sale.id]) == [(date(2026, 11, 1), 3000)]
    rows = sorted(session.scalars(select(Installment).where(Installment.sale_id == sale.id)),
                  key=lambda row: row.installment_index)
    # The 9,000 had paid installment 1 and part of 2; the later 3,000 stays on installment 2.
    assert [row.amount_paid for row in rows[:3]] == [0, 3000, 0]


def test_returning_a_payment_needs_the_owner_and_a_reason(session: Session, owner_id: int) -> None:
    seller = auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    )
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type="credit",
        wholesale_price=24000, cash_price=30000, purchase_date=DAY,
    )
    (payment,) = payments.record_credit_payment(session, sale.id, 5000, date(2026, 9, 5), "cash"),
    with pytest.raises(auth.AuthorizationError):
        payments.reverse_payment(session, seller.id, payment.id, "x")
    with pytest.raises(ValueError):
        payments.reverse_payment(session, owner_id, payment.id, "  ")
    payments.reverse_payment(session, owner_id, payment.id, "أعيد المبلغ للزبون")
    assert session.scalars(select(Payment)).all() == []


# ================================================================== screens
@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> dict:
    """Migrated file database with an owner, a seller, two stocked products and a client."""
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: ("", ""))
    from app.db.migrate import upgrade_database

    upgrade_database()
    seed_defaults()
    with session_scope() as session:
        owner = auth.create_first_owner(
            session, username="akrem", password="Secret123", password_confirmation="Secret123"
        )
        seller = auth.create_seller(
            session, owner.id, username="vendeur", password="Secret123", password_confirmation="Secret123"
        )
        iphone = products.create_product(session, owner.id, name="13 PRO", wholesale_price=80000, cash_price=90000)
        redmi = products.create_product(session, owner.id, name="Redmi 13", wholesale_price=25000, cash_price=32000)
        stock.add_units(session, owner.id, product_id=iphone.id, quantity=2)
        stock.add_units(session, owner.id, product_id=redmi.id, quantity=3)
        client = customers.create_customer(
            session, full_name="أكرم", phone="0550001122", category_id=categories.get_fallback_type(session).id
        )
        ids = {"owner": owner.id, "seller": seller.id, "iphone": iphone.id, "redmi": redmi.id, "client": client.id}
    yield ids
    dispose_database_engines()


def _user(user_id: int) -> User:
    with session_scope() as session:
        return session.get(User, user_id)


def _stock_left(shop: dict) -> tuple[int, int]:
    with session_scope() as session:
        return len(stock.available_units(session, shop["iphone"])), len(stock.available_units(session, shop["redmi"]))


def test_a_credit_basket_with_scanned_imeis_takes_both_phones_from_stock(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page._set_cash_status("credit")
    page.cash_customer_combo.setCurrentIndex(page.cash_customer_combo.findData(shop["client"]))
    for name, imei in (("13 PRO", "356789012345678"), ("Redmi 13", "356789012345679")):
        page.cash_product.setCurrentText(name)
        page.cash_details.scan.setText(imei)  # the phone's barcode; stock was entered without IMEIs
        page.cash_details.scan.returnPressed.emit()
        page._add_cash_to_basket()
    page._save_cash_sale()

    assert _stock_left(shop) == (1, 2)
    with session_scope() as session:
        saved = session.scalars(select(Sale).where(Sale.customer_id == shop["client"])).all()
        assert {sale.sale_type for sale in saved} == {"credit"} and len(saved) == 2


def test_a_phone_no_longer_picked_leaves_no_ref_behind(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["owner"]))
    page.cash_product.setCurrentText("Redmi 13")
    page.cash_details.unit.setCurrentIndex(1)
    assert page.cash_details.reference.text().startswith("REF-")

    page.cash_product.setCurrentText("13 PRO")  # another product: that Redmi is not sold
    assert page.cash_details.reference.text() == "" and page.cash_details.imei.text() == ""
    page._save_cash_sale()
    assert _stock_left(shop) == (1, 3)


def test_a_sellers_installment_basket_saves_only_the_products_added(shop) -> None:
    from app.ui.pages.new_sale_page import NewSalePage

    page = NewSalePage(current_user=_user(shop["seller"]))
    page._customer_id = shop["client"]
    page._switch_module(1)
    page.product.setCurrentIndex(page.product.findText("Redmi 13"))
    page._add_sale_to_basket()
    assert page.product.currentText() == ""
    page.product.setCurrentIndex(page.product.findText("Redmi 13"))
    page._add_sale_to_basket()
    page._save_sale()

    with session_scope() as session:
        saved = session.scalars(select(Sale).where(Sale.customer_id == shop["client"])).all()
        assert [sale.product for sale in saved] == ["Redmi 13", "Redmi 13"]
    assert _stock_left(shop) == (2, 1)


def test_the_owner_changes_the_payment_method_from_the_client_window(shop, monkeypatch) -> None:
    from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog
    from app.ui.dialogs.payment_type_dialog import PaymentTypeDialog

    with session_scope() as session:
        sale = sales.create_sale(
            session, customer_id=shop["client"], product="Redmi 13", sale_type="credit",
            wholesale_price=25000, cash_price=32000, down_payment=2000, purchase_date=DAY,
        )
        payments.record_credit_payment(session, sale.id, 6000, date(2026, 9, 15), "cash")
        sale_id = sale.id

    def choose_installment(dialog: PaymentTypeDialog) -> QDialog.DialogCode:
        dialog.sale_type.setCurrentIndex(dialog.sale_type.findData("installment"))
        dialog.months.setValue(4)
        assert dialog.down_payment.value() == 2000 and dialog.received.isVisibleTo(dialog)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(PaymentTypeDialog, "exec", choose_installment)
    window = CustomerDetailsDialog(shop["client"], _user(shop["owner"]))
    assert window.change_type_button.isVisibleTo(window)
    window._change_payment_type()

    with session_scope() as session:
        changed = session.get(Sale, sale_id)
        assert (changed.sale_type, changed.months, changed.rate) == ("installment", 4, 35)
        assert sum(row.amount_paid for row in changed.installments) == 6000


def test_the_owner_returns_a_payment_from_the_client_window(shop, monkeypatch) -> None:
    from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog

    with session_scope() as session:
        sale = sales.create_sale(
            session, customer_id=shop["client"], product="Redmi 13", sale_type="credit",
            wholesale_price=25000, cash_price=32000, purchase_date=DAY,
        )
        payments.record_credit_payment(session, sale.id, 6000, date(2026, 9, 15), "cash")
        payments.record_credit_payment(session, sale.id, 4000, date(2026, 9, 25), "cash")
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *args, **kwargs: ("خطأ", True))

    window = CustomerDetailsDialog(shop["client"], _user(shop["owner"]))
    assert window.reverse_payment_button.isVisibleTo(window)
    window.receipt_history.setCurrentIndex(1)  # newest first: the 6,000 of 15/09
    window._reverse_selected_payment()

    with session_scope() as session:
        assert [payment.amount for payment in session.scalars(select(Payment))] == [4000]
    seller_window = CustomerDetailsDialog(shop["client"], _user(shop["seller"]))
    assert not seller_window.reverse_payment_button.isVisibleTo(seller_window)


# ================================================================== returning a product
def _return_session_sale(session: Session, owner_id: int, sale_type: str = "installment") -> tuple[int, Sale]:
    product_id = _product(session, owner_id, "Redmi 13", 2)
    extra = {"rate": 35, "months": 6} if sale_type == "installment" else {}
    sale = sales.create_sale(
        session, customer_id=_customer(session), product="Redmi 13", sale_type=sale_type,
        wholesale_price=24000, cash_price=30000, down_payment=0 if sale_type == "cash" else 5000,
        purchase_date=DAY, **extra,
    )
    return product_id, sale


def test_a_returned_product_goes_back_to_stock_and_is_owed_no_more(session: Session, owner_id: int) -> None:
    product_id, sale = _return_session_sale(session, owner_id)
    payments.record_sale_payment(session, sale.id, 6000, date(2026, 10, 1), "cash")
    assert _available(session, product_id) == 1

    sales.return_sales(session, owner_id, [sale.id], refund=9000, return_date=date(2026, 10, 5), reason="عطب")

    session.refresh(sale)
    assert _available(session, product_id) == 2
    assert (sale.returned_at, sale.refund_amount, sale.return_reason) == (date(2026, 10, 5), 9000, "عطب")
    # 11,000 received, 9,000 given back: the shop kept 2,000, its whole profit (the phone is back).
    assert (sale.sale_type, sale.total, sale.profit, sale.wholesale_price) == ("cash", 2000, 2000, 0)
    assert sale.installments == [] and session.scalars(select(Payment)).all() == []
    history = customers.customer_history(session, sale.customer_id, today=date(2026, 10, 5))
    (purchase,) = history.purchases
    assert (purchase.sale_type, purchase.remaining, history.remaining) == ("returned", 0, 0)
    assert [(item.refund, item.reason) for item in purchase.returns] == [(9000, "عطب")]
    audit = session.scalar(select(AuditLog).where(AuditLog.action == "sale_returned"))
    assert audit is not None and '"total": 40500' in audit.details


def test_the_refund_defaults_to_everything_paid(session: Session, owner_id: int) -> None:
    _product_id, sale = _return_session_sale(session, owner_id, "cash")
    sales.return_sales(session, owner_id, [sale.id])
    session.refresh(sale)
    assert (sale.refund_amount, sale.total) == (30000, 0)
    (purchase,) = customers.customer_history(session, sale.customer_id, today=DAY).purchases
    assert purchase.transactions == ()


def test_one_product_of_a_basket_can_be_returned(session: Session, owner_id: int) -> None:
    redmi = _product(session, owner_id, "Redmi 13", 2)
    case = _product(session, owner_id, "Case", 2, cash=2000)
    client = _customer(session)
    phone, cover = sales.create_sales_batch(session, owner_id, customer_id=client, lines=[
        _line("Redmi 13", "credit"), _line("Case", "credit", cash=2000),
    ])

    sales.return_sales(session, owner_id, [cover.id])

    assert (_available(session, redmi), _available(session, case)) == (1, 2)
    rows = customers.list_customers(session, year=2026, month=9, today=DAY)
    (row,) = [row for row in rows if row.id == client]
    assert row.display_kinds == ("credit", "returned") and row.remaining_balance == 30000


def test_a_return_is_owner_only_and_happens_once(session: Session, owner_id: int) -> None:
    seller = auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    )
    _product_id, sale = _return_session_sale(session, owner_id, "credit")
    with pytest.raises(auth.AuthorizationError):
        sales.return_sales(session, seller.id, [sale.id])
    with pytest.raises(ValueError):
        sales.return_sales(session, owner_id, [sale.id], refund=5001)  # only 5,000 was paid
    sales.return_sales(session, owner_id, [sale.id])
    with pytest.raises(ValueError):
        sales.return_sales(session, owner_id, [sale.id])
    with pytest.raises(ValueError):
        sales.change_sale_type(session, owner_id, [sale.id], "credit")
    with pytest.raises(ValueError):
        sales.update_sale(session, sale.id, product="Other")


def test_the_owner_returns_a_product_from_the_client_window(shop, monkeypatch) -> None:
    from app.ui.dialogs.customer_details_dialog import CustomerDetailsDialog
    from app.ui.dialogs.return_dialog import ReturnDialog

    with session_scope() as session:
        sales.create_sale(
            session, customer_id=shop["client"], product="Redmi 13", sale_type="credit",
            wholesale_price=25000, cash_price=32000, down_payment=10000, purchase_date=DAY,
        )
    assert _stock_left(shop) == (2, 2)

    def give_back_8000(dialog: ReturnDialog) -> QDialog.DialogCode:
        assert dialog.refund.value() == 10000  # what the client paid
        dialog.refund.setValue(8000)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ReturnDialog, "exec", give_back_8000)
    window = CustomerDetailsDialog(shop["client"], _user(shop["owner"]))
    assert window.return_button.isVisibleTo(window)
    window._return_products()

    assert _stock_left(shop) == (2, 3)
    assert not window.return_button.isVisibleTo(window) and not window.change_type_button.isVisibleTo(window)
    with session_scope() as session:
        (returned,) = session.scalars(select(Sale).where(Sale.customer_id == shop["client"])).all()
        assert (returned.refund_amount, returned.total) == (8000, 2000)
