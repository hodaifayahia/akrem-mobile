"""Owner requests of 2026-10-03 (second round): weekly WhatsApp reminders, barcode/IMEI
scanning, the sellers' discount limit, and the shop server shared by several PCs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Category, Sale
from app.db.session import session_scope
from app.services import auth, customers, products, reminders, sales, settings, shared_db, stock
from app.services.seed import seed_defaults

PASSWORD = "StrongPass123"
TODAY = date(2026, 10, 3)


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


@pytest.fixture
def seller_id(session: Session, owner_id: int) -> int:
    return auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    ).id


def _customer(session: Session, name: str, phone: str | None = "0661234567") -> int:
    category = session.scalar(select(Category).limit(1))
    return customers.create_customer(session, full_name=name, phone=phone, category_id=category.id).id


def _installment(session: Session, customer_id: int, purchase: date, product: str = "Redmi 13") -> Sale:
    return sales.create_sale(
        session, customer_id=customer_id, product=product, sale_type="installment",
        wholesale_price=20000, cash_price=30000, rate=0, months=3, purchase_date=purchase,
    )


# ------------------------------------------------------------------ weekly reminders
def test_weekly_reminders_group_each_client_and_put_late_ones_first(session: Session) -> None:
    soon = _customer(session, "قريب", "0550000001")
    late = _customer(session, "متأخر", "0550000002")
    no_phone = _customer(session, "بدون هاتف", None)
    later = _customer(session, "بعيد", "0550000003")
    # Due on 06/10 (within the week): two sales of the same client.
    _installment(session, soon, date(2026, 9, 6))
    _installment(session, soon, date(2026, 9, 6), "Case")
    _installment(session, late, date(2026, 8, 1))           # 01/09 and 01/10: late
    _installment(session, no_phone, date(2026, 9, 8))
    _installment(session, later, date(2026, 9, 20))         # 20/10: not this week

    targets = reminders.weekly_targets(session, today=TODAY)

    assert [target.customer_name for target in targets] == ["متأخر", "قريب", "بدون هاتف"]
    late_target, soon_target, phoneless = targets
    assert late_target.late and late_target.amount == 20000
    assert soon_target.amount == 20000 and soon_target.products == ("Redmi 13", "Case")
    assert soon_target.first_due == date(2026, 10, 6)
    assert not phoneless.can_send and reminders.link_for(phoneless) is None
    assert reminders.link_for(soon_target).startswith("https://wa.me/213550000001?text=")
    assert "20,000" in reminders.message_for(soon_target)
    # Without the late ones, the late client keeps only 01/10 (still within the grace days).
    this_week = reminders.weekly_targets(session, today=TODAY, include_late=False)
    assert [(t.customer_name, t.late, t.amount) for t in this_week] == [
        ("متأخر", False, 10000), ("قريب", False, 20000), ("بدون هاتف", False, 10000),
    ]


def test_sent_reminders_are_logged_and_shown(session: Session, owner_id: int) -> None:
    client = _customer(session, "قريب")
    _installment(session, client, date(2026, 9, 6))
    assert reminders.weekly_targets(session, today=TODAY)[0].last_reminded is None

    reminders.record_reminder(session, owner_id, client, amount=10000)

    target = reminders.weekly_targets(session, today=TODAY)[0]
    assert target.last_reminded is not None
    assert target.reminded_on(target.last_reminded.date())
    entry = session.scalars(select(AuditLog).where(AuditLog.action == reminders.REMINDER_ACTION)).one()
    assert (entry.entity, entry.entity_id, entry.user_id) == ("customer", client, owner_id)


# ------------------------------------------------------------------ barcode / IMEI
def test_a_scan_finds_the_phone_by_imei_or_ref(session: Session, owner_id: int) -> None:
    product = products.create_product(session, owner_id, name="Redmi 13", cash_price=32000)
    (unit,) = stock.add_units(
        session, owner_id, product_id=product.id, details=stock.UnitDetails(imei="356789012345678")
    )
    number = int(unit.reference.split("-")[1])

    assert stock.find_by_scan(session, "356789012345678").id == unit.id
    assert stock.find_by_scan(session, " 35678 9012345678 ").id == unit.id
    assert stock.find_by_scan(session, unit.reference.lower()).id == unit.id
    assert stock.find_by_scan(session, f"REF{number}").id == unit.id
    assert stock.find_by_scan(session, str(number)).id == unit.id
    assert stock.find_by_scan(session, "999999999999999") is None
    assert stock.find_by_scan(session, "") is None
    unit.status = stock.STATUS_SOLD
    assert stock.find_by_scan(session, "356789012345678") is None  # sold phones are not offered


def test_scanning_imeis_adds_one_phone_each(session: Session, owner_id: int) -> None:
    product = products.create_product(session, owner_id, name="Redmi 13", cash_price=32000, wholesale_price=25000)
    valid, rejected = stock.scanned_imeis("356789012345678\n\n356789012345679\n356789012345678\nabc\n12")
    assert valid == ["356789012345678", "356789012345679"] and rejected == ["abc", "12"]

    added = stock.add_scanned_units(
        session, owner_id, product_id=product.id, imeis=valid, details=stock.UnitDetails(color="Black"),
    )

    assert [unit.imei for unit in added] == valid
    assert {unit.color for unit in added} == {"Black"} and {unit.cash_price for unit in added} == {32000}
    assert len({unit.reference for unit in added}) == 2
    with pytest.raises(stock.StockError):  # already in stock: nothing is added
        stock.add_scanned_units(session, owner_id, product_id=product.id, imeis=["356789012345680", valid[0]])
    assert len(stock.available_units(session, product.id)) == 2


# ------------------------------------------------------------------ seller discount
def _catalog(session: Session, owner_id: int) -> int:
    """The catalog product the discount tests sell, with phones in stock (every sale takes one)."""
    product_id = products.create_product(
        session, owner_id, name="Redmi 13", cash_price=40000, wholesale_price=30000
    ).id
    stock.add_units(session, owner_id, product_id=product_id, quantity=10)
    return product_id


def _seller_sale(session: Session, seller_id: int, client: int, **values: object) -> Sale:
    line = {"product": "Redmi 13", "sale_type": "cash", "wholesale_price": 0, "rate": 0, "purchase_date": TODAY}
    line.update(values)
    return sales.create_sale_for_user(session, seller_id, customer_id=client, **line)


def test_a_seller_may_discount_up_to_the_limit(session: Session, owner_id: int, seller_id: int) -> None:
    _catalog(session, owner_id)
    client = _customer(session, "زبون")
    assert settings.get_seller_max_discount(session) == settings.DEFAULT_SELLER_MAX_DISCOUNT == 5

    sale = _seller_sale(session, seller_id, client, cash_price=38000)  # exactly 5%
    assert (sale.cash_price, sale.wholesale_price) == (38000, 30000)
    assert _seller_sale(session, seller_id, client, cash_price=0).cash_price == 40000  # blank = catalog
    assert _seller_sale(session, seller_id, client, cash_price=45000).cash_price == 45000  # above is fine

    with pytest.raises(sales.DiscountApprovalRequired) as refused:
        _seller_sale(session, seller_id, client, cash_price=37900)
    assert (refused.value.percent, refused.value.limit) == (6, 5)


def test_a_bigger_discount_needs_the_owner_and_is_logged(session: Session, owner_id: int, seller_id: int) -> None:
    _catalog(session, owner_id)
    client = _customer(session, "زبون")
    settings.set_seller_max_discount(session, owner_id, 2)
    with pytest.raises(auth.AuthorizationError):
        settings.set_seller_max_discount(session, seller_id, 50)

    with pytest.raises(sales.DiscountApprovalRequired):
        _seller_sale(session, seller_id, client, cash_price=36000, approved_by_owner_id=seller_id)
    assert auth.approve_as_owner(session, username="seller", password=PASSWORD) is None
    assert auth.approve_as_owner(session, username="owner", password="wrong") is None
    owner = auth.approve_as_owner(session, username="owner", password=PASSWORD)
    assert owner is not None and owner.id == owner_id

    sale = _seller_sale(session, seller_id, client, cash_price=36000, approved_by_owner_id=owner_id)

    entry = session.scalars(select(AuditLog).where(AuditLog.action == "discount_approved")).one()
    assert (entry.entity_id, entry.user_id) == (sale.id, owner_id)
    assert '"percent": 10' in entry.details


def test_lowering_the_installment_rate_counts_as_a_discount(session: Session, owner_id: int, seller_id: int) -> None:
    _catalog(session, owner_id)
    client = _customer(session, "زبون")
    # Preset for 6 months is 35%: 40,000 → 54,000. At 25% the total is 50,000 (7.4% less).
    plan = {"sale_type": "installment", "months": 6, "cash_price": 40000}
    assert _seller_sale(session, seller_id, client, rate=35, **plan).total == 54000
    with pytest.raises(sales.DiscountApprovalRequired) as refused:
        _seller_sale(session, seller_id, client, rate=25, **plan)
    assert refused.value.percent == 8
    assert _seller_sale(session, seller_id, client, rate=33, **plan).rate == 33  # 1.5% less: allowed


def test_the_owner_has_no_discount_limit(session: Session, owner_id: int) -> None:
    _catalog(session, owner_id)
    client = _customer(session, "زبون")
    sale = sales.create_sale_for_user(
        session, owner_id, customer_id=client, product="Redmi 13", sale_type="cash",
        wholesale_price=30000, cash_price=20000, purchase_date=TODAY,
    )
    assert sale.cash_price == 20000


def test_a_basket_passes_the_owner_approval_to_every_line(session: Session, owner_id: int, seller_id: int) -> None:
    _catalog(session, owner_id)
    client = _customer(session, "زبون")
    lines = [
        {"product": "Redmi 13", "sale_type": "cash", "wholesale_price": 0, "cash_price": 30000, "purchase_date": TODAY},
        {"product": "Redmi 13", "sale_type": "cash", "wholesale_price": 0, "cash_price": 40000, "purchase_date": TODAY},
    ]
    with pytest.raises(sales.DiscountApprovalRequired):
        sales.create_sales_batch(session, seller_id, customer_id=client, lines=lines)
    assert session.scalars(select(Sale)).all() == []
    saved = sales.create_sales_batch(
        session, seller_id, customer_id=client, lines=lines, approved_by_owner_id=owner_id
    )
    assert [sale.cash_price for sale in saved] == [30000, 40000]


# ------------------------------------------------------------------ shop server settings
def test_server_details_build_a_safe_url_and_are_kept_per_pc(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    from app.config import database_url

    details = shared_db.ServerDetails(host="192.168.1.10", password="p@ss:w/rd")
    url = details.url()
    assert url.startswith("postgresql+psycopg://akrem:") and "@192.168.1.10:5432/akremmobile" in url
    assert "p@ss" not in url  # escaped
    with pytest.raises(ValueError):
        shared_db.ServerDetails(host=" ", password="x").url()
    with pytest.raises(ValueError):
        shared_db.ServerDetails(host="pc", password="x", port=70000).url()

    assert shared_db.current_server() is None and not shared_db.is_shared()
    assert database_url().startswith("sqlite")
    shared_db.use_server(details)
    assert shared_db.current_server() == details
    assert database_url() == url and shared_db.is_shared()
    shared_db.use_this_pc()
    assert shared_db.current_server() is None and database_url().startswith("sqlite")


def test_an_unreachable_server_is_reported_without_raising() -> None:
    check = shared_db.test_connection(shared_db.ServerDetails(host="127.0.0.1", port=1, password="x"))
    assert check.code == "unreachable" and not check.ok
    assert shared_db.test_connection(shared_db.ServerDetails(host="", password="x")).code == "error"


# ------------------------------------------------------------------ a real PostgreSQL server (optional)
POSTGRES_URL = os.environ.get("AKREMMOBILE_TEST_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="set AKREMMOBILE_TEST_POSTGRES_URL to an empty test database")
def test_sharing_with_a_real_server_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Copy this PC's records to the server, use it, back it up, restore it."""
    from sqlalchemy.engine import make_url

    from app.db.migrate import upgrade_database
    from app.db.session import dispose_database_engines
    from app.services import backup

    parsed = make_url(POSTGRES_URL)
    details = shared_db.ServerDetails(
        host=parsed.host, port=parsed.port or 5432, database=parsed.database,
        user=parsed.username, password=parsed.password or "",
    )
    monkeypatch.setenv("AKREMMOBILE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKREMMOBILE_DATABASE_URL", raising=False)
    # This PC's local records.
    upgrade_database()
    seed_defaults()
    with session_scope() as local:
        owner = auth.create_first_owner(local, username="owner", password=PASSWORD, password_confirmation=PASSWORD)
        client = _customer(local, "زبون الخادم")
        _installment(local, client, date(2026, 9, 6))
        owner_id = owner.id
    dispose_database_engines()

    check = shared_db.test_connection(details)
    assert check.ok and not check.has_data, check.detail
    shared_db.prepare_server(details)
    counts = shared_db.copy_local_data(details)
    assert counts["customers"] == 1 and counts["installments"] == 3
    assert shared_db.test_connection(details).has_data
    with pytest.raises(ValueError):
        shared_db.copy_local_data(details)  # never merged twice

    shared_db.use_server(details)
    try:
        with session_scope() as server:
            assert server.get_bind().dialect.name == "postgresql"
            assert [c.full_name for c in customers.list_customers(server, year=2026, month=10, today=TODAY)] == [
                "زبون الخادم"
            ]
            second = _customer(server, "زبون ثان", None)  # ids keep counting after the copy
            assert second > client
            # A payment takes the server's ledger lock (PostgreSQL only).
            sale_id = server.scalar(select(Sale.id).where(Sale.customer_id == client))
            from app.services import payments

            payments.record_sale_payment_for_user(server, owner_id, sale_id, 15000, TODAY, "cash", None)
        saved = backup.create_backup(tmp_path / "server-backups")
        backup.validate_backup(saved)
        with session_scope() as server:
            customers.create_customer(server, full_name="بعد النسخة", category_id=server.scalar(
                select(Category.id).limit(1)))
        backup.restore_backup(saved)
        with session_scope() as server:
            names = sorted(c.full_name for c in customers.list_customers(server, year=2026, month=10, today=TODAY))
            assert names == ["زبون الخادم", "زبون ثان"]
            assert auth.approve_as_owner(server, username="owner", password=PASSWORD).id == owner_id
            _customer(server, "بعد الاسترجاع", None)  # sequences were reset after the restore
    finally:
        shared_db.use_this_pc()
        dispose_database_engines()
