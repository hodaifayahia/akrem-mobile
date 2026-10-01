"""SQLAlchemy models for the installment manager."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base for all persisted models."""


class Category(Base):
    """Editable customer category, shown to the owner as a client type."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    color: Mapped[str] = mapped_column(String(16), nullable=False, default="slate", server_default="slate")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    customers: Mapped[list[Customer]] = relationship(back_populates="category", passive_deletes=True)


class Product(Base):
    """Owner-managed price book used by sellers when recording a sale."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("wholesale_price >= 0", name="ck_products_wholesale_nonnegative"),
        CheckConstraint("cash_price >= 0", name="ck_products_cash_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    wholesale_price: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_price: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Customer(Base):
    """A customer and their identifying/contact information."""

    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_full_name", "full_name"),
        Index("ix_customers_phone", "phone"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(180), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    id_number: Mapped[str | None] = mapped_column(String(60))
    id_issue_date: Mapped[date | None] = mapped_column(Date)
    id_issue_place: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(240))
    profession: Mapped[str | None] = mapped_column(String(120))
    ccp_number: Mapped[str | None] = mapped_column(String(60))
    cheques_count: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    category: Mapped[Category] = relationship(back_populates="customers")
    sales: Mapped[list[Sale]] = relationship(back_populates="customer", passive_deletes=True)


class Sale(Base):
    """A cash, installment, or credit sale."""

    __tablename__ = "sales"
    __table_args__ = (
        CheckConstraint("sale_type IN ('cash', 'installment', 'credit')", name="ck_sales_type"),
        CheckConstraint("wholesale_price >= 0", name="ck_sales_wholesale_nonnegative"),
        CheckConstraint("cash_price >= 0", name="ck_sales_cash_nonnegative"),
        CheckConstraint("down_payment >= 0", name="ck_sales_down_payment_nonnegative"),
        CheckConstraint("rate >= 0 AND rate <= 100", name="ck_sales_rate_range"),
        CheckConstraint("months IS NULL OR months >= 1", name="ck_sales_months_positive"),
        Index("ix_sales_customer_id", "customer_id"),
        Index("ix_sales_purchase_date", "purchase_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    product: Mapped[str] = mapped_column(String(180), nullable=False)
    sale_type: Mapped[str] = mapped_column(String(20), nullable=False)
    wholesale_price: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_price: Mapped[int] = mapped_column(Integer, nullable=False)
    rate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    down_payment: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    months: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    financed: Mapped[int] = mapped_column(Integer, nullable=False)
    monthly_amount: Mapped[int | None] = mapped_column(Integer)
    profit: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    expected_pay_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    customer: Mapped[Customer] = relationship(back_populates="sales")
    installments: Mapped[list[Installment]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", passive_deletes=True
    )
    credit_payments: Mapped[list[Payment]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", passive_deletes=True
    )


class Installment(Base):
    """A scheduled monthly amount due for an installment sale."""

    __tablename__ = "installments"
    __table_args__ = (
        UniqueConstraint("sale_id", "installment_index", name="uq_installments_sale_index"),
        CheckConstraint("installment_index >= 1", name="ck_installments_index_positive"),
        CheckConstraint("amount_due >= 0 AND amount_paid >= 0", name="ck_installments_amounts_nonnegative"),
        Index("ix_installments_due_date", "due_date"),
        Index("ix_installments_sale_id", "sale_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), nullable=False)
    installment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_due: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_paid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paid_date: Mapped[date | None] = mapped_column(Date)
    method: Mapped[str | None] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text)

    sale: Mapped[Sale] = relationship(back_populates="installments")
    payments: Mapped[list[Payment]] = relationship(
        back_populates="installment", cascade="all, delete-orphan", passive_deletes=True
    )


class Payment(Base):
    """A received amount, including partial payments against an installment."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "(installment_id IS NOT NULL AND sale_id IS NULL) OR "
            "(installment_id IS NULL AND sale_id IS NOT NULL)",
            name="ck_payments_exactly_one_target",
        ),
        Index("ix_payments_installment_id", "installment_id"),
        Index("ix_payments_payment_date", "payment_date"),
        Index("ix_payments_sale_id", "sale_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    installment_id: Mapped[int | None] = mapped_column(ForeignKey("installments.id", ondelete="CASCADE"))
    sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"))
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    installment: Mapped[Installment | None] = relationship(back_populates="payments")
    sale: Mapped[Sale | None] = relationship(back_populates="credit_payments")


class Setting(Base):
    """Persisted application setting; values are JSON-encoded strings."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    """Application login account."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('owner', 'seller')", name="ck_users_role"),
        Index("ix_users_username", "username", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="seller")
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Two-step verification (RFC 6238 TOTP). A secret with totp_enabled False is a
    # setup still waiting for its first confirmation code.
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    totp_last_counter: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    audit_entries: Mapped[list[AuditLog]] = relationship(back_populates="user", passive_deletes=True)


class AuditLog(Base):
    """Audit record for sensitive changes such as payment reversals."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_entity", "entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    entity: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

    user: Mapped[User | None] = relationship(back_populates="audit_entries")
