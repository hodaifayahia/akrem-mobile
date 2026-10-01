"""Client type (customer category) management service."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Category, Customer
from app.db.session import session_scope
from app.services import auth, categories, repositories
from app.services.categories import (
    FALLBACK_TYPE_NAME,
    TYPE_COLORS,
    ClientTypeError,
    assign_client_type,
    create_client_type,
    delete_client_type,
    get_fallback_type,
    list_client_types,
    reorder_client_types,
    update_client_type,
)
from app.services.seed import DEFAULT_CATEGORIES, seed_defaults

PASSWORD = "StrongPass123"


@pytest.fixture
def session(memory_engine: Engine) -> Iterator[Session]:
    """Seeded session that is committed on success like application code."""
    with session_scope(memory_engine) as scoped:
        seed_defaults(scoped)
        yield scoped


@pytest.fixture
def owner_id(session: Session) -> int:
    """Create the first owner account."""
    owner = auth.create_first_owner(
        session, username="owner", password=PASSWORD, password_confirmation=PASSWORD
    )
    return owner.id


@pytest.fixture
def seller_id(session: Session, owner_id: int) -> int:
    """Create a seller account."""
    seller = auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    )
    return seller.id


def _type(session: Session, name: str) -> Category:
    category = session.scalar(select(Category).where(Category.name == name))
    assert category is not None
    return category


def _customer(session: Session, category_id: int, name: str = "Client") -> Customer:
    return repositories.create_customer(session, full_name=name, category_id=category_id)


def _code(error: pytest.ExceptionInfo[ClientTypeError]) -> str:
    return error.value.code


# --- Seed --------------------------------------------------------------------


def test_seed_values(session: Session) -> None:
    """New databases get the five types with documented colors and the system flag."""
    rows = {
        category.name: (category.color, category.sort_order, category.is_system, category.is_default)
        for category in session.scalars(select(Category))
    }
    assert rows == {
        "أساتذة": ("blue", 1, False, True),
        "منحة البطالة": ("amber", 2, False, True),
        "عسكري": ("green", 3, False, True),
        "أعمال حرة": ("violet", 4, False, True),
        "غير مصنف": ("slate", 99, True, True),
    }
    assert DEFAULT_CATEGORIES[-1] == FALLBACK_TYPE_NAME
    assert set(TYPE_COLORS) >= {color for color, *_ in rows.values()}


def test_seed_does_not_touch_existing_types(session: Session, owner_id: int) -> None:
    """Re-seeding leaves owner colors and order alone."""
    teachers = _type(session, "أساتذة")
    update_client_type(session, owner_id, teachers.id, color="rose")
    seed_defaults(session)
    assert teachers.color == "rose"
    assert session.scalar(select(func.count()).select_from(Category)) == 5


# --- Listing -----------------------------------------------------------------


def test_list_client_types_order_and_counts(session: Session, owner_id: int) -> None:
    """Owner types are ordered by sort order, the system type is always last."""
    military = _type(session, "عسكري")
    _customer(session, military.id, "A")
    _customer(session, military.id, "B")
    custom = create_client_type(session, owner_id, name="تجار", color="teal")

    types = list_client_types(session)
    assert [item.name for item in types] == [
        "أساتذة", "منحة البطالة", "عسكري", "أعمال حرة", "تجار", FALLBACK_TYPE_NAME,
    ]
    by_name = {item.name: item for item in types}
    assert by_name["عسكري"].customer_count == 2
    assert by_name["تجار"].customer_count == 0
    assert by_name["تجار"].color == "teal"
    assert by_name["تجار"].sort_order == 5
    assert by_name["تجار"].is_default is False
    assert by_name[FALLBACK_TYPE_NAME].is_system is True
    assert custom.id == by_name["تجار"].id


def test_list_breaks_sort_order_ties_by_name(session: Session) -> None:
    """Equal sort orders fall back to name, then id."""
    for name in ("أساتذة", "منحة البطالة", "عسكري", "أعمال حرة"):
        _type(session, name).sort_order = 1
    session.flush()
    names = [item.name for item in list_client_types(session)]
    assert names[:4] == sorted(["أساتذة", "منحة البطالة", "عسكري", "أعمال حرة"])
    assert names[-1] == FALLBACK_TYPE_NAME


# --- Fallback ----------------------------------------------------------------


def test_get_fallback_type_returns_system_row(session: Session) -> None:
    """The seeded system row is returned even if it was recolored."""
    fallback = get_fallback_type(session)
    assert fallback.name == FALLBACK_TYPE_NAME
    assert fallback.is_system is True


def test_get_fallback_type_creates_when_missing(memory_engine: Engine) -> None:
    """An empty database gets a fresh system fallback type."""
    with session_scope(memory_engine) as session:
        fallback = get_fallback_type(session)
        assert (fallback.name, fallback.color, fallback.sort_order) == (FALLBACK_TYPE_NAME, "slate", 99)
        assert fallback.is_system is True
        assert fallback.is_default is True
        assert get_fallback_type(session).id == fallback.id


def test_get_fallback_type_promotes_existing_named_row(memory_engine: Engine) -> None:
    """A non-system row with the fallback name is promoted instead of duplicated."""
    with session_scope(memory_engine) as session:
        legacy = Category(name=FALLBACK_TYPE_NAME, is_default=True)
        session.add(legacy)
        session.flush()
        fallback = get_fallback_type(session)
        assert fallback.id == legacy.id
        assert fallback.is_system is True
        assert session.scalar(select(func.count()).select_from(Category)) == 1


# --- Authorization -----------------------------------------------------------


def test_mutations_require_owner(session: Session, owner_id: int, seller_id: int) -> None:
    """Sellers, unknown users, and disabled owners cannot change client types."""
    teachers = _type(session, "أساتذة")
    military = _type(session, "عسكري")
    customer = _customer(session, teachers.id)
    ordered = [item.id for item in list_client_types(session) if not item.is_system]
    calls = [
        lambda user: create_client_type(session, user, name="جديد"),
        lambda user: update_client_type(session, user, teachers.id, color="rose"),
        lambda user: delete_client_type(session, user, military.id),
        lambda user: assign_client_type(session, user, [customer.id], military.id),
        lambda user: reorder_client_types(session, user, ordered),
    ]
    for call in calls:
        with pytest.raises(auth.AuthorizationError):
            call(seller_id)
        with pytest.raises(auth.AuthorizationError):
            call(999_999)
    assert teachers.color == "blue"
    assert customer.category_id == teachers.id
    assert session.get(Category, military.id) is not None


# --- Create ------------------------------------------------------------------


def test_create_client_type_trims_and_appends(session: Session, owner_id: int) -> None:
    """Names are trimmed and new types go after the last non-system type."""
    first = create_client_type(session, owner_id, name="  تجار  ")
    second = create_client_type(session, owner_id, name="فلاحون", color="orange")
    assert first.name == "تجار"
    assert first.color == "blue"
    assert first.is_system is False
    assert (first.sort_order, second.sort_order) == (5, 6)
    assert second.color == "orange"


def test_create_client_type_validation(session: Session, owner_id: int) -> None:
    """Empty, too long, duplicate, and bad-color inputs raise coded errors."""
    cases = [
        ({"name": "   "}, "empty_name"),
        ({"name": "x" * 81}, "name_too_long"),
        ({"name": "اساتذه"}, "duplicate_name"),
        ({"name": " أساتذة "}, "duplicate_name"),
        ({"name": "غير  مصنف"}, "duplicate_name"),
        ({"name": "جديد", "color": "pink"}, "invalid_color"),
    ]
    for kwargs, code in cases:
        with pytest.raises(ClientTypeError) as error:
            create_client_type(session, owner_id, **kwargs)
        assert _code(error) == code
        assert isinstance(error.value, ValueError)
    assert create_client_type(session, owner_id, name="x" * 80).name == "x" * 80


# --- Update ------------------------------------------------------------------


def test_update_client_type_rename_and_recolor(session: Session, owner_id: int) -> None:
    """Owners can rename and recolor types; renaming to its own name is allowed."""
    teachers = _type(session, "أساتذة")
    update_client_type(session, owner_id, teachers.id, name="أساتذة", color="teal")
    assert (teachers.name, teachers.color) == ("أساتذة", "teal")
    update_client_type(session, owner_id, teachers.id, name="  معلمون ")
    assert teachers.name == "معلمون"
    assert teachers.color == "teal"


def test_update_client_type_errors(session: Session, owner_id: int) -> None:
    """Unknown ids, duplicates, bad colors, and empty names are rejected."""
    teachers = _type(session, "أساتذة")
    with pytest.raises(ClientTypeError) as error:
        update_client_type(session, owner_id, 999_999, name="x")
    assert _code(error) == "not_found"
    with pytest.raises(ClientTypeError) as error:
        update_client_type(session, owner_id, teachers.id, name="منحه البطاله")
    assert _code(error) == "duplicate_name"
    with pytest.raises(ClientTypeError) as error:
        update_client_type(session, owner_id, teachers.id, name="")
    assert _code(error) == "empty_name"
    with pytest.raises(ClientTypeError) as error:
        update_client_type(session, owner_id, teachers.id, color="black")
    assert _code(error) == "invalid_color"


def test_system_type_can_be_recolored_but_not_renamed(session: Session, owner_id: int) -> None:
    """The fallback type keeps its name."""
    fallback = get_fallback_type(session)
    with pytest.raises(ClientTypeError) as error:
        update_client_type(session, owner_id, fallback.id, name="آخرون")
    assert _code(error) == "system_type"
    update_client_type(session, owner_id, fallback.id, name=FALLBACK_TYPE_NAME, color="rose")
    assert (fallback.name, fallback.color) == (FALLBACK_TYPE_NAME, "rose")


# --- Delete ------------------------------------------------------------------


def test_delete_unused_client_type(session: Session, owner_id: int) -> None:
    """An unused type is deleted and no customers are moved."""
    military = _type(session, "عسكري")
    assert delete_client_type(session, owner_id, military.id) == 0
    assert session.get(Category, military.id) is None


def test_delete_used_client_type_requires_reassign(session: Session, owner_id: int) -> None:
    """A used type needs a valid reassignment target, then its customers move."""
    military = _type(session, "عسكري")
    freelance = _type(session, "أعمال حرة")
    moved_customers = [_customer(session, military.id, f"C{index}") for index in range(3)]
    other = _customer(session, freelance.id, "Other")

    with pytest.raises(ClientTypeError) as error:
        delete_client_type(session, owner_id, military.id)
    assert _code(error) == "in_use"
    with pytest.raises(ClientTypeError) as error:
        delete_client_type(session, owner_id, military.id, reassign_to_id=military.id)
    assert _code(error) == "invalid_reassign"
    with pytest.raises(ClientTypeError) as error:
        delete_client_type(session, owner_id, military.id, reassign_to_id=999_999)
    assert _code(error) == "invalid_reassign"

    assert delete_client_type(session, owner_id, military.id, reassign_to_id=freelance.id) == 3
    session.expire_all()
    assert session.get(Category, military.id) is None
    for customer in moved_customers:
        assert session.get(Customer, customer.id).category_id == freelance.id
    assert session.get(Customer, other.id).category_id == freelance.id
    counts = {item.name: item.customer_count for item in list_client_types(session)}
    assert counts["أعمال حرة"] == 4


def test_delete_client_type_errors(session: Session, owner_id: int) -> None:
    """Unknown and system types cannot be deleted."""
    with pytest.raises(ClientTypeError) as error:
        delete_client_type(session, owner_id, 999_999)
    assert _code(error) == "not_found"
    fallback = get_fallback_type(session)
    with pytest.raises(ClientTypeError) as error:
        delete_client_type(session, owner_id, fallback.id, reassign_to_id=_type(session, "عسكري").id)
    assert _code(error) == "system_type"


def test_delete_can_reassign_to_system_type(session: Session, owner_id: int) -> None:
    """Customers of a deleted type can be moved into the fallback type."""
    military = _type(session, "عسكري")
    customer = _customer(session, military.id)
    fallback = get_fallback_type(session)
    assert delete_client_type(session, owner_id, military.id, reassign_to_id=fallback.id) == 1
    assert customer.category_id == fallback.id


# --- Assign ------------------------------------------------------------------


def test_assign_client_type_counts_only_changes(session: Session, owner_id: int) -> None:
    """Bulk assignment skips customers already of the type and unknown ids."""
    teachers = _type(session, "أساتذة")
    military = _type(session, "عسكري")
    first = _customer(session, teachers.id, "A")
    second = _customer(session, teachers.id, "B")
    already = _customer(session, military.id, "C")

    changed = assign_client_type(
        session, owner_id, [first.id, second.id, already.id, first.id, 999_999], military.id
    )
    assert changed == 2
    assert {first.category_id, second.category_id, already.category_id} == {military.id}
    assert assign_client_type(session, owner_id, iter([first.id]), military.id) == 0


def test_assign_client_type_errors(session: Session, owner_id: int) -> None:
    """Unknown type and empty/unknown customer selections raise coded errors."""
    teachers = _type(session, "أساتذة")
    customer = _customer(session, teachers.id)
    with pytest.raises(ClientTypeError) as error:
        assign_client_type(session, owner_id, [customer.id], 999_999)
    assert _code(error) == "not_found"
    with pytest.raises(ClientTypeError) as error:
        assign_client_type(session, owner_id, [], teachers.id)
    assert _code(error) == "no_customers"
    with pytest.raises(ClientTypeError) as error:
        assign_client_type(session, owner_id, [999_998, 999_999], teachers.id)
    assert _code(error) == "no_customers"


# --- Reorder -----------------------------------------------------------------


def test_reorder_client_types(session: Session, owner_id: int) -> None:
    """Sort orders become 1..n in the given order; the system type stays last."""
    custom = create_client_type(session, owner_id, name="تجار")
    current = [item.id for item in list_client_types(session) if not item.is_system]
    new_order = [custom.id, *reversed(current[:-1])]
    reorder_client_types(session, owner_id, new_order)
    types = list_client_types(session)
    assert [item.id for item in types[:-1]] == new_order
    assert [item.sort_order for item in types[:-1]] == [1, 2, 3, 4, 5]
    assert types[-1].is_system is True
    assert types[-1].sort_order == 99


def test_reorder_client_types_rejects_wrong_sets(session: Session, owner_id: int) -> None:
    """The ids must be exactly the non-system types, each once."""
    current = [item.id for item in list_client_types(session) if not item.is_system]
    fallback_id = get_fallback_type(session).id
    for bad in (current[:-1], [*current, fallback_id], [*current, current[0]], [*current[:-1], 999_999]):
        with pytest.raises(ClientTypeError) as error:
            reorder_client_types(session, owner_id, bad)
        assert _code(error) == "invalid_order"
    assert [item.id for item in list_client_types(session) if not item.is_system] == current


# --- Legacy API ----------------------------------------------------------------


def test_legacy_category_helpers_still_work(session: Session) -> None:
    """The original category functions keep working with the new columns."""
    category = categories.create_category(session, "قديم")
    assert category.color == "slate"
    assert category.sort_order == 0
    assert category.is_system is False
    categories.rename_category(session, category.id, "قديم 2")
    categories.delete_category(session, category.id)
    assert session.get(Category, category.id) is None


# --- Migration 0003 --------------------------------------------------------------


def test_migration_0003_styles_existing_rows(tmp_path: Path) -> None:
    """Upgrading an existing 0002 database styles defaults and orders custom types."""
    url = f"sqlite+pysqlite:///{(tmp_path / 'upgrade.sqlite3').as_posix()}"
    config = Config(str(Path(__file__).resolve().parents[1] / "app" / "db" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0002_product_catalog")

    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for name in ("زبائن VIP", *DEFAULT_CATEGORIES, "تجار"):
                connection.execute(
                    text("INSERT INTO categories (name, is_default) VALUES (:name, 1)"), {"name": name}
                )
            connection.execute(text(
                "INSERT INTO customers (category_id, full_name) "
                "SELECT id, 'Existing' FROM categories WHERE name = 'زبائن VIP'"
            ))

        command.upgrade(config, "head")
        with engine.connect() as connection:
            rows = {
                name: (color, sort_order, bool(is_system))
                for name, color, sort_order, is_system in connection.execute(
                    text("SELECT name, color, sort_order, is_system FROM categories")
                )
            }
            customers = connection.scalar(text("SELECT count(*) FROM customers"))
        assert rows == {
            "أساتذة": ("blue", 1, False),
            "منحة البطالة": ("amber", 2, False),
            "عسكري": ("green", 3, False),
            "أعمال حرة": ("violet", 4, False),
            "غير مصنف": ("slate", 99, True),
            "زبائن VIP": ("slate", 10, False),
            "تجار": ("slate", 11, False),
        }
        assert customers == 1

        command.downgrade(config, "0002_product_catalog")
        with engine.connect() as connection:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(categories)"))}
            assert connection.scalar(text("SELECT count(*) FROM categories")) == 7
        assert {"color", "sort_order", "is_system"}.isdisjoint(columns)
    finally:
        engine.dispose()
