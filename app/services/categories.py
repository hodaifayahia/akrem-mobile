"""Category (client type) management and list queries for the Customers screen.

The ``Category`` model is presented to the owner as a "client type". The legacy
``*_category`` helpers keep their original behavior; the ``*_client_type`` API
adds owner authorization, colors, ordering, and the protected fallback type.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Category, Customer
from app.services import auth, repositories
from app.services.customers import normalize_search_text


def list_categories(session: Session) -> list[tuple[Category, int]]:
    """Return categories with customer counts, including categories with none."""
    rows = session.execute(
        select(Category, func.count(Customer.id))
        .outerjoin(Customer, Customer.category_id == Category.id)
        .group_by(Category.id)
        .order_by(Category.name, Category.id)
    )
    return [(category, int(count)) for category, count in rows]


def create_category(session: Session, name: str, *, is_default: bool = False) -> Category:
    """Create a named category through the shared repository validation."""
    return repositories.create_category(session, name, is_default=is_default)


def rename_category(session: Session, category_id: int, name: str) -> Category:
    """Rename an existing category through the shared repository validation."""
    return repositories.rename_category(session, category_id, name)


def delete_category(session: Session, category_id: int) -> None:
    """Delete a category only when it has no customers."""
    repositories.delete_category(session, category_id)


# --- Client types -----------------------------------------------------------

TYPE_COLORS: tuple[str, ...] = ("blue", "teal", "green", "amber", "orange", "rose", "violet", "slate")
FALLBACK_TYPE_NAME = "غير مصنف"
MAX_TYPE_NAME_LENGTH = 80
_FALLBACK_SORT_ORDER = 99


class ClientTypeError(ValueError):
    """Validation error with a stable machine-readable ``code`` for UI translation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ClientType:
    """Read-only view of a client type and how many customers use it."""

    id: int
    name: str
    color: str
    sort_order: int
    is_system: bool
    is_default: bool
    customer_count: int


def list_client_types(session: Session) -> list[ClientType]:
    """Return client types: owner types by (sort_order, name, id), system types last."""
    rows = list_categories(session)
    types = [_to_client_type(category, count) for category, count in rows]
    return sorted(types, key=lambda item: (item.is_system, item.sort_order, item.name, item.id))


def get_fallback_type(session: Session) -> Category:
    """Return the protected fallback type, promoting or creating it when missing."""
    system_type = session.scalar(
        select(Category).where(Category.is_system.is_(True)).order_by(Category.id).limit(1)
    )
    if system_type is not None:
        return system_type
    named = session.scalar(select(Category).where(Category.name == FALLBACK_TYPE_NAME))
    if named is not None:
        named.is_system = True
        session.flush()
        return named
    fallback = Category(
        name=FALLBACK_TYPE_NAME,
        color="slate",
        sort_order=_FALLBACK_SORT_ORDER,
        is_system=True,
        is_default=True,
    )
    session.add(fallback)
    session.flush()
    return fallback


def create_client_type(
    session: Session, owner_user_id: int, *, name: str, color: str = "blue"
) -> Category:
    """Create an owner-defined type placed after the existing non-system types."""
    auth.require_owner(session, owner_user_id)
    clean_name = _validated_name(session, name)
    clean_color = _validated_color(color)
    max_order = session.scalar(
        select(func.max(Category.sort_order)).where(Category.is_system.is_(False))
    )
    category = Category(
        name=clean_name,
        color=clean_color,
        sort_order=(max_order or 0) + 1,
        is_system=False,
        is_default=False,
    )
    session.add(category)
    session.flush()
    return category


def update_client_type(
    session: Session,
    owner_user_id: int,
    type_id: int,
    *,
    name: str | None = None,
    color: str | None = None,
) -> Category:
    """Rename and/or recolor a type; system types may only change color."""
    auth.require_owner(session, owner_user_id)
    category = _require_type(session, type_id)
    if name is not None:
        if category.is_system and name.strip() != category.name:
            raise ClientTypeError("system_type", "The system client type cannot be renamed")
        category.name = _validated_name(session, name, exclude_id=category.id)
    if color is not None:
        category.color = _validated_color(color)
    session.flush()
    return category


def delete_client_type(
    session: Session,
    owner_user_id: int,
    type_id: int,
    *,
    reassign_to_id: int | None = None,
) -> int:
    """Delete a non-system type, optionally moving its customers first.

    Returns the number of customers moved to ``reassign_to_id``.
    """
    auth.require_owner(session, owner_user_id)
    category = _require_type(session, type_id)
    if category.is_system:
        raise ClientTypeError("system_type", "The system client type cannot be deleted")
    target = _reassign_target(session, type_id, reassign_to_id)
    used = _customer_count(session, type_id)
    if used and target is None:
        raise ClientTypeError("in_use", "The client type still has customers")
    moved = 0
    if used and target is not None:
        moved = _move_customers(session, type_id, target)
    session.delete(category)
    session.flush()
    return moved


def assign_client_type(
    session: Session, owner_user_id: int, customer_ids: Iterable[int], type_id: int
) -> int:
    """Set the type of many customers; returns how many actually changed."""
    auth.require_owner(session, owner_user_id)
    category = _require_type(session, type_id)
    unique_ids = list(dict.fromkeys(customer_ids))
    customers = (
        session.scalars(select(Customer).where(Customer.id.in_(unique_ids))).all()
        if unique_ids else []
    )
    if not customers:
        raise ClientTypeError("no_customers", "No matching customers were selected")
    changed = 0
    for customer in customers:
        if customer.category_id != category.id:
            customer.category = category
            changed += 1
    session.flush()
    return changed


def reorder_client_types(
    session: Session, owner_user_id: int, ordered_ids: Sequence[int]
) -> None:
    """Assign sort orders 1..n following ``ordered_ids`` (exactly the non-system types)."""
    auth.require_owner(session, owner_user_id)
    categories = session.scalars(select(Category).where(Category.is_system.is_(False))).all()
    by_id = {category.id: category for category in categories}
    ids = list(ordered_ids)
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ClientTypeError(
            "invalid_order", "The new order must list every non-system client type exactly once"
        )
    for position, type_id in enumerate(ids, start=1):
        by_id[type_id].sort_order = position
    session.flush()


def _to_client_type(category: Category, customer_count: int) -> ClientType:
    """Build the immutable view of one category row."""
    return ClientType(
        id=category.id,
        name=category.name,
        color=category.color,
        sort_order=category.sort_order,
        is_system=bool(category.is_system),
        is_default=bool(category.is_default),
        customer_count=customer_count,
    )


def _require_type(session: Session, type_id: int) -> Category:
    """Return the type or raise ``not_found``."""
    category = session.get(Category, type_id)
    if category is None:
        raise ClientTypeError("not_found", "Client type not found")
    return category


def _name_key(name: str) -> str:
    """Comparison key so spelling variants and spacing count as the same name."""
    return " ".join(normalize_search_text(name).split())


def _validated_name(session: Session, name: str, *, exclude_id: int | None = None) -> str:
    """Trim a type name and enforce non-empty, length, and normalized uniqueness."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ClientTypeError("empty_name", "Client type name cannot be empty")
    if len(clean_name) > MAX_TYPE_NAME_LENGTH:
        raise ClientTypeError("name_too_long", "Client type name is too long")
    key = _name_key(clean_name)
    for type_id, existing_name in session.execute(select(Category.id, Category.name)):
        if type_id != exclude_id and _name_key(existing_name) == key:
            raise ClientTypeError("duplicate_name", "A client type with this name already exists")
    return clean_name


def _validated_color(color: str) -> str:
    """Return the color when it is one of the supported palette names."""
    if color not in TYPE_COLORS:
        raise ClientTypeError("invalid_color", "Unsupported client type color")
    return color


def _reassign_target(session: Session, type_id: int, reassign_to_id: int | None) -> Category | None:
    """Validate the optional reassignment target of a delete."""
    if reassign_to_id is None:
        return None
    target = session.get(Category, reassign_to_id) if reassign_to_id != type_id else None
    if target is None:
        raise ClientTypeError("invalid_reassign", "Choose another existing client type")
    return target


def _customer_count(session: Session, type_id: int) -> int:
    """Count customers currently linked to a type."""
    return int(session.scalar(
        select(func.count()).select_from(Customer).where(Customer.category_id == type_id)
    ) or 0)


def _move_customers(session: Session, source_id: int, target: Category) -> int:
    """Move every customer of ``source_id`` to ``target`` and return how many moved."""
    customers = session.scalars(select(Customer).where(Customer.category_id == source_id)).all()
    for customer in customers:
        customer.category = target
    session.flush()
    return len(customers)
