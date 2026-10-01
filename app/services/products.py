"""Owner-managed product price book for sales entry."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Product, User
from app.services import auth


def list_products(session: Session, *, include_inactive: bool = False) -> list[Product]:
    """Return catalog items alphabetically, hiding archived entries by default."""
    query = select(Product)
    if not include_inactive:
        query = query.where(Product.active.is_(True))
    return list(session.scalars(query.order_by(Product.name, Product.id)))


def create_product(
    session: Session,
    owner_user_id: int,
    *,
    name: str,
    wholesale_price: int,
    cash_price: int,
) -> Product:
    """Create a catalog product after verifying the active owner account."""
    auth.require_owner(session, owner_user_id)
    clean_name = _validate_name(name)
    _validate_amount("wholesale price", wholesale_price)
    _validate_amount("cash price", cash_price)
    product = Product(
        name=clean_name,
        wholesale_price=wholesale_price,
        cash_price=cash_price,
        active=True,
    )
    session.add(product)
    try:
        session.flush()
    except IntegrityError as error:
        raise ValueError("A catalog product already uses this name") from error
    return product


def update_product(
    session: Session,
    owner_user_id: int,
    product_id: int,
    *,
    name: str,
    wholesale_price: int,
    cash_price: int,
) -> Product:
    """Update the active catalog price while old sales keep their stored snapshot."""
    auth.require_owner(session, owner_user_id)
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError("Catalog product not found")
    product.name = _validate_name(name)
    product.wholesale_price = _validate_amount("wholesale price", wholesale_price)
    product.cash_price = _validate_amount("cash price", cash_price)
    try:
        session.flush()
    except IntegrityError as error:
        raise ValueError("A catalog product already uses this name") from error
    return product


def set_product_active(
    session: Session,
    owner_user_id: int,
    product_id: int,
    active: bool,
) -> Product:
    """Activate or archive a catalog item without changing historical sales."""
    auth.require_owner(session, owner_user_id)
    if not isinstance(active, bool):
        raise ValueError("Product active state must be boolean")
    product = session.get(Product, product_id)
    if product is None:
        raise ValueError("Catalog product not found")
    product.active = active
    session.flush()
    return product


def find_active_product(session: Session, name: str) -> Product | None:
    """Find an active catalog entry by its trimmed exact product name."""
    clean_name = name.strip() if isinstance(name, str) else ""
    if not clean_name:
        return None
    return session.scalar(
        select(Product).where(Product.name == clean_name, Product.active.is_(True))
    )


def _validate_name(name: str) -> str:
    """Trim a non-empty product name to the database length limit."""
    if not isinstance(name, str):
        raise ValueError("Product name is required")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Product name cannot be empty")
    if len(clean_name) > 180:
        raise ValueError("Product name cannot exceed 180 characters")
    return clean_name


def _validate_amount(label: str, value: int) -> int:
    """Validate whole-dinar prices and return the normalized number."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label.capitalize()} must be a non-negative whole number")
    return value
