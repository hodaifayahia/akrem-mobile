"""Persistence operations for categories, customers, and sales."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Category, Customer, Sale


def create_category(session: Session, name: str, *, is_default: bool = False) -> Category:
    """Create a category with a trimmed, non-empty name."""
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Category name cannot be empty")
    category = Category(name=clean_name, is_default=is_default)
    session.add(category)
    session.flush()
    return category


def rename_category(session: Session, category_id: int, name: str) -> Category:
    """Rename a category."""
    category = session.get(Category, category_id)
    if category is None:
        raise ValueError("Category not found")
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Category name cannot be empty")
    category.name = clean_name
    session.flush()
    return category


def delete_category(session: Session, category_id: int) -> None:
    """Delete a category only when no customers use it."""
    category = session.get(Category, category_id)
    if category is None:
        raise ValueError("Category not found")
    uses = session.scalar(select(func.count()).select_from(Customer).where(
        Customer.category_id == category_id
    ))
    if uses:
        raise ValueError("Cannot delete a category that has customers")
    session.delete(category)
    session.flush()


def create_customer(
    session: Session,
    *,
    full_name: str,
    category_id: int,
    phone: str | None = None,
    **details: Any,
) -> Customer:
    """Create a customer, trimming required text fields."""
    clean_name = full_name.strip()
    if not clean_name:
        raise ValueError("Customer name cannot be empty")
    customer = Customer(full_name=clean_name, category_id=category_id, phone=phone, **details)
    session.add(customer)
    session.flush()
    return customer


def update_customer(session: Session, customer_id: int, **changes: Any) -> Customer:
    """Update a customer's supplied fields."""
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise ValueError("Customer not found")
    if "full_name" in changes:
        changes["full_name"] = changes["full_name"].strip()
        if not changes["full_name"]:
            raise ValueError("Customer name cannot be empty")
    for field, value in changes.items():
        if not hasattr(Customer, field) or field in {"id", "created_at", "sales"}:
            raise ValueError(f"Unsupported customer field: {field}")
        setattr(customer, field, value)
    session.flush()
    return customer


def delete_customer(session: Session, customer_id: int) -> None:
    """Delete only customers without sales to preserve financial history."""
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise ValueError("Customer not found")
    if customer.sales:
        raise ValueError("Cannot delete a customer who has sales")
    session.delete(customer)
    session.flush()


def create_sale(
    session: Session,
    *,
    customer_id: int,
    product: str,
    sale_type: str,
    wholesale_price: int,
    cash_price: int,
    rate: int,
    down_payment: int,
    months: int | None,
    total: int,
    financed: int,
    monthly_amount: int | None,
    profit: int,
    purchase_date: date,
    end_date: date | None = None,
    expected_pay_date: date | None = None,
    payment_interval: int = 1,
) -> Sale:
    """Create a sale record using already-computed integer amounts."""
    sale = Sale(
        customer_id=customer_id,
        product=product.strip(),
        sale_type=sale_type,
        wholesale_price=wholesale_price,
        cash_price=cash_price,
        rate=rate,
        down_payment=down_payment,
        months=months,
        payment_interval=payment_interval,
        total=total,
        financed=financed,
        monthly_amount=monthly_amount,
        profit=profit,
        purchase_date=purchase_date,
        end_date=end_date,
        expected_pay_date=expected_pay_date,
    )
    if not sale.product:
        raise ValueError("Product cannot be empty")
    session.add(sale)
    session.flush()
    return sale


def update_sale(session: Session, sale_id: int, **changes: Any) -> Sale:
    """Update a sale's supplied fields."""
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise ValueError("Sale not found")
    for field, value in changes.items():
        if not hasattr(Sale, field) or field in {"id", "created_at", "customer", "installments"}:
            raise ValueError(f"Unsupported sale field: {field}")
        setattr(sale, field, value)
    session.flush()
    return sale


def delete_sale(session: Session, sale_id: int) -> None:
    """Delete a sale and its installment/payment history."""
    sale = session.get(Sale, sale_id)
    if sale is None:
        raise ValueError("Sale not found")
    session.delete(sale)
    session.flush()
