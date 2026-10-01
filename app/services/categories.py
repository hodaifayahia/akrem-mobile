"""Category management and list queries for the Customers screen."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Category, Customer
from app.services import repositories


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
