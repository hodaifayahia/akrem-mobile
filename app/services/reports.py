"""Read-only monthly collection and owner profit reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.models import Installment, Sale, Setting
from app.services import auth
from app.services.status import Status, for_month


@dataclass(frozen=True, slots=True)
class CollectionRow:
    """One installment plus customer and sale details for a report."""

    customer: str
    phone: str
    product: str
    sale_type: str
    due_date: date
    amount_due: int
    amount_paid: int
    remaining: int
    status: Status


@dataclass(frozen=True, slots=True)
class OverdueRow:
    """One unpaid installment past its configured grace period."""

    customer: str
    phone: str
    product: str
    due_date: date
    amount_due: int
    amount_paid: int
    remaining: int
    days_late: int


@dataclass(frozen=True, slots=True)
class ProfitRow:
    """Sale totals grouped by the sale's purchase month."""

    year: int
    month: int
    sale_count: int
    wholesale: int
    total: int
    profit: int


def monthly_collection_rows(
    session: Session,
    *,
    year: int,
    month: int,
    today: date,
) -> list[CollectionRow]:
    """Return installment rows due in a selected month and their payment state."""
    if not 1 <= month <= 12:
        raise ValueError("month must be between 1 and 12")
    grace = _grace_days(session)
    installments = session.scalars(
        select(Installment)
        .options(
            selectinload(Installment.payments),
            joinedload(Installment.sale).joinedload(Sale.customer),
        )
        .where(
            Installment.due_date >= date(year, month, 1),
            Installment.due_date < _first_next_month(year, month),
        )
        .order_by(Installment.due_date, Installment.sale_id, Installment.installment_index)
    ).all()
    rows: list[CollectionRow] = []
    for installment in installments:
        sale = installment.sale
        customer = sale.customer
        paid = max(installment.amount_paid, sum(payment.amount for payment in installment.payments))
        rows.append(
            CollectionRow(
                customer.full_name,
                customer.phone or "",
                sale.product,
                sale.sale_type,
                installment.due_date,
                installment.amount_due,
                paid,
                max(0, installment.amount_due - paid),
                for_month([installment], year, month, today, grace),
            )
        )
    return rows


def overdue_rows(session: Session, *, today: date) -> list[OverdueRow]:
    """Return every installment that remains unpaid after due date and grace."""
    grace = _grace_days(session)
    installments = session.scalars(
        select(Installment)
        .options(
            selectinload(Installment.payments),
            joinedload(Installment.sale).joinedload(Sale.customer),
        )
        .where(Installment.due_date <= today - timedelta(days=grace + 1))
        .order_by(Installment.due_date, Installment.sale_id, Installment.installment_index)
    ).all()
    rows: list[OverdueRow] = []
    for installment in installments:
        paid = max(installment.amount_paid, sum(payment.amount for payment in installment.payments))
        remaining = max(0, installment.amount_due - paid)
        if remaining <= 0:
            continue
        sale = installment.sale
        customer = sale.customer
        rows.append(
            OverdueRow(
                customer.full_name,
                customer.phone or "",
                sale.product,
                installment.due_date,
                installment.amount_due,
                paid,
                remaining,
                (today - installment.due_date).days,
            )
        )
    return rows


def profit_by_month(
    session: Session,
    *,
    start_year: int,
    end_year: int,
    owner_user_id: int,
) -> list[ProfitRow]:
    """Aggregate sales and profit by purchase month for a verified owner."""
    auth.require_owner(session, owner_user_id)
    if start_year < 1 or end_year < start_year:
        raise ValueError("Invalid report year range")
    rows = session.scalars(
        select(Sale)
        .where(Sale.purchase_date >= date(start_year, 1, 1))
        .where(Sale.purchase_date < date(end_year + 1, 1, 1))
        .order_by(Sale.purchase_date, Sale.id)
    ).all()
    groups: dict[tuple[int, int], list[Sale]] = {}
    for sale in rows:
        groups.setdefault((sale.purchase_date.year, sale.purchase_date.month), []).append(sale)
    return [
        ProfitRow(
            year,
            month,
            len(sales),
            sum(sale.wholesale_price for sale in sales),
            sum(sale.total for sale in sales),
            sum(sale.profit for sale in sales),
        )
        for (year, month), sales in sorted(groups.items())
    ]


def export_collection_xlsx(rows: Iterable[CollectionRow], destination: str | Path) -> Path:
    """Write a monthly collection sheet with printable widths and Arabic headings."""
    from app.i18n import ar

    matrix = [[
        ar.REPORT_CUSTOMER, ar.REPORT_PHONE, ar.REPORT_PRODUCT, ar.REPORT_TYPE,
        ar.REPORT_DUE_DATE, ar.REPORT_DUE, ar.REPORT_PAID, ar.REPORT_REMAINING,
        ar.REPORT_STATUS,
    ]]
    labels = {
        Status.PAID: ar.CUST_STATUS_TIP_PAID,
        Status.FAILED: ar.CUST_STATUS_TIP_FAILED,
        Status.PENDING: ar.CUST_STATUS_TIP_PENDING,
        Status.NONE: ar.CUST_STATUS_TIP_NONE,
    }
    for row in rows:
        matrix.append([
            row.customer,
            row.phone,
            row.product,
            _sale_type_label(row.sale_type),
            row.due_date.strftime("%d/%m/%Y"),
            row.amount_due,
            row.amount_paid,
            row.remaining,
            labels[row.status],
        ])
    return _write_xlsx(matrix, destination, ar.REPORT_COLLECTION_TITLE)


def export_overdue_xlsx(rows: Iterable[OverdueRow], destination: str | Path) -> Path:
    """Write a list of outstanding past-due installments."""
    from app.i18n import ar

    matrix = [[
        ar.REPORT_CUSTOMER, ar.REPORT_PHONE, ar.REPORT_PRODUCT, ar.REPORT_DUE_DATE,
        ar.REPORT_DUE, ar.REPORT_PAID, ar.REPORT_REMAINING, ar.REPORT_DAYS_LATE,
    ]]
    matrix.extend([
        row.customer,
        row.phone,
        row.product,
        row.due_date.strftime("%d/%m/%Y"),
        row.amount_due,
        row.amount_paid,
        row.remaining,
        row.days_late,
    ] for row in rows)
    return _write_xlsx(matrix, destination, ar.REPORT_OVERDUE_TITLE)


def export_profit_xlsx(rows: Iterable[ProfitRow], destination: str | Path) -> Path:
    """Write an owner-only month-by-month sales/profit report."""
    from app.i18n import ar

    matrix = [[
        ar.REPORT_YEAR, ar.REPORT_MONTH, ar.REPORT_SALE_COUNT,
        ar.REPORT_WHOLESALE, ar.REPORT_TOTAL, ar.REPORT_PROFIT,
    ]]
    matrix.extend([
        row.year,
        f"{row.month:02d}",
        row.sale_count,
        row.wholesale,
        row.total,
        row.profit,
    ] for row in rows)
    return _write_xlsx(matrix, destination, ar.REPORT_PROFIT_TITLE)


def _write_xlsx(matrix: list[list[object]], destination: str | Path, title: str) -> Path:
    """Write a right-to-left workbook with a styled header and frozen first row."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title[:31]
    worksheet.sheet_view.rightToLeft = True
    for row in matrix:
        worksheet.append(row)
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="0758CD")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for column in worksheet.columns:
        values = [len(str(cell.value or "")) for cell in column]
        width = min(36, max(12, max(values, default=0) + 2))
        worksheet.column_dimensions[get_column_letter(column[0].column)].width = width
    workbook.save(path)
    return path


def _grace_days(session: Session) -> int:
    """Read the business grace period from settings with the five-day default."""
    setting = session.get(Setting, "grace_days")
    if setting is None:
        return 5
    try:
        value = json.loads(setting.value)
    except (TypeError, json.JSONDecodeError):
        return 5
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 5


def _first_next_month(year: int, month: int) -> date:
    """Return the first day after a selected month."""
    return date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)


def _sale_type_label(value: str) -> str:
    """Translate a stored sale type for a report workbook."""
    from app.i18n import ar

    return {
        "cash": ar.SALE_CASH,
        "installment": ar.SALE_INSTALLMENT,
        "credit": ar.SALE_CREDIT,
    }.get(value, value)


# --- Customer list and full data exports ---------------------------------------

_DATE_FORMAT = "dd/mm/yyyy"
_MONEY_FORMAT = "#,##0"


def export_customers_xlsx(
    summaries: Iterable[object],
    destination: str | Path,
    *,
    status_labels: dict[Status, str] | None = None,
) -> Path:
    """Write the (filtered) customer list shown on the Customers screen."""
    from app.i18n import ar

    labels = status_labels or {
        Status.PAID: ar.CUST_STATUS_TIP_PAID,
        Status.FAILED: ar.CUST_STATUS_TIP_FAILED,
        Status.PENDING: ar.CUST_STATUS_TIP_PENDING,
        Status.NONE: ar.CUST_STATUS_TIP_NONE,
    }
    header = [
        ar.CUST_COL_NAME, ar.CUST_COL_PHONE, ar.CUST_COL_CATEGORY, ar.CUST_COL_PAYMENT_KIND,
        ar.CUST_COL_PRODUCTS, ar.CUST_COL_PURCHASE_DATE, ar.CUST_COL_END_DATE,
        ar.CUST_COL_STATUS, ar.CUST_COL_MONTHLY_DUE, ar.CUST_COL_REMAINING,
    ]
    rows = [
        [
            summary.full_name,
            summary.phone or "",
            summary.category_name,
            "، ".join(_sale_type_label(kind) for kind in summary.sale_types),
            "، ".join(summary.products),
            summary.purchase_date,
            summary.end_date,
            labels[summary.status],
            summary.monthly_amount,
            summary.remaining_balance,
        ]
        for summary in summaries
    ]
    return _write_workbook(destination, [(ar.CUST_PAGE_TITLE, header, rows, {5, 6}, {8, 9})])


def export_full_workbook(session: Session, destination: str | Path, *, owner_user_id: int) -> Path:
    """Export every customer, client type, sale, installment and payment (owner only).

    The workbook contains wholesale prices and profit, so it is restricted to
    an active owner like the profit report.
    """
    from app.db.models import Category, Customer, Payment
    from app.i18n import ar

    auth.require_owner(session, owner_user_id)
    customers = session.scalars(
        select(Customer).options(joinedload(Customer.category)).order_by(Customer.full_name, Customer.id)
    ).all()
    sales = session.scalars(
        select(Sale).options(joinedload(Sale.customer)).order_by(Sale.purchase_date, Sale.id)
    ).all()
    installments = session.scalars(
        select(Installment)
        .options(joinedload(Installment.sale).joinedload(Sale.customer))
        .order_by(Installment.sale_id, Installment.installment_index)
    ).all()
    payments = session.scalars(
        select(Payment)
        .options(
            joinedload(Payment.installment).joinedload(Installment.sale).joinedload(Sale.customer),
            joinedload(Payment.sale).joinedload(Sale.customer),
        )
        .order_by(Payment.payment_date, Payment.id)
    ).all()
    type_rows = session.execute(
        select(Category.name, Category.color, Category.is_system).order_by(Category.sort_order, Category.name)
    ).all()
    type_counts: dict[str, int] = {}
    for customer in customers:
        type_counts[customer.category.name] = type_counts.get(customer.category.name, 0) + 1

    sheets = [
        (
            ar.EXPORT_SHEET_CUSTOMERS,
            [ar.EXPORT_COL_ID, ar.CUST_COL_NAME, ar.CUST_COL_PHONE, ar.CUST_COL_CATEGORY,
             ar.CUST_ID_NUMBER, ar.CUST_ID_ISSUE_DATE, ar.CUST_ID_ISSUE_PLACE, ar.CUST_ADDRESS,
             ar.CUST_PROFESSION, ar.CUST_CCP_NUMBER, ar.CUST_CHEQUES_COUNT, ar.CUST_NOTES],
            [[c.id, c.full_name, c.phone or "", c.category.name, c.id_number or "", c.id_issue_date,
              c.id_issue_place or "", c.address or "", c.profession or "", c.ccp_number or "",
              c.cheques_count, c.notes or ""] for c in customers],
            {5}, set(),
        ),
        (
            ar.EXPORT_SHEET_SALES,
            [ar.EXPORT_COL_ID, ar.CUST_COL_NAME, ar.REPORT_PRODUCT, ar.REPORT_TYPE,
             ar.REPORT_WHOLESALE, ar.CASH_PRICE_DETAIL, ar.RATE, ar.EXPORT_COL_DOWN_PAYMENT,
             ar.MONTHS_DURATION, ar.TOTAL_AFTER_INSTALLMENT, ar.EXPORT_COL_FINANCED,
             ar.MONTHLY_AMOUNT, ar.TOTAL_PROFIT, ar.CUST_COL_PURCHASE_DATE, ar.CUST_COL_END_DATE,
             ar.EXPORT_COL_EXPECTED_DATE],
            [[s.id, s.customer.full_name, s.product, _sale_type_label(s.sale_type), s.wholesale_price,
              s.cash_price, s.rate, s.down_payment, s.months, s.total, s.financed, s.monthly_amount,
              s.profit, s.purchase_date, s.end_date, s.expected_pay_date] for s in sales],
            {13, 14, 15}, {4, 5, 7, 9, 10, 11, 12},
        ),
        (
            ar.EXPORT_SHEET_INSTALLMENTS,
            [ar.EXPORT_COL_SALE_ID, ar.CUST_COL_NAME, ar.REPORT_PRODUCT, ar.EXPORT_COL_INDEX,
             ar.REPORT_DUE_DATE, ar.REPORT_DUE, ar.REPORT_PAID, ar.REPORT_REMAINING,
             ar.EXPORT_COL_PAID_DATE, ar.EXPORT_COL_METHOD],
            [[i.sale_id, i.sale.customer.full_name, i.sale.product, i.installment_index, i.due_date,
              i.amount_due, i.amount_paid, max(0, i.amount_due - i.amount_paid), i.paid_date,
              i.method or ""] for i in installments],
            {4, 8}, {5, 6, 7},
        ),
        (
            ar.EXPORT_SHEET_PAYMENTS,
            [ar.EXPORT_COL_ID, ar.CUST_COL_NAME, ar.REPORT_PRODUCT, ar.REPORT_TYPE, ar.EXPORT_COL_AMOUNT,
             ar.EXPORT_COL_PAID_DATE, ar.EXPORT_COL_METHOD, ar.CUST_NOTES],
            [_payment_row(payment) for payment in payments],
            {5}, {4},
        ),
        (
            ar.CT_TITLE,
            [ar.CT_NAME, ar.CT_COLOR, ar.EXPORT_COL_CUSTOMERS],
            [[name, color, type_counts.get(name, 0)] for name, color, _system in type_rows],
            set(), set(),
        ),
    ]
    return _write_workbook(destination, sheets)


def _payment_row(payment: object) -> list[object]:
    sale = payment.installment.sale if payment.installment is not None else payment.sale
    return [
        payment.id,
        sale.customer.full_name if sale is not None else "",
        sale.product if sale is not None else "",
        _sale_type_label(sale.sale_type) if sale is not None else "",
        payment.amount,
        payment.payment_date,
        payment.method,
        payment.note or "",
    ]


def _write_workbook(
    destination: str | Path,
    sheets: list[tuple[str, list[str], list[list[object]], set[int], set[int]]],
) -> Path:
    """Write right-to-left sheets with real Excel dates and grouped whole-dinar amounts.

    Each sheet is ``(title, header, rows, date_columns, money_columns)`` with
    zero-based column indexes.
    """
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, header, rows, date_columns, money_columns in sheets:
        worksheet = workbook.create_sheet(_sheet_title(title))
        worksheet.sheet_view.rightToLeft = True
        worksheet.append(header)
        for row in rows:
            worksheet.append(row)
        for column_index in date_columns | money_columns:
            letter = get_column_letter(column_index + 1)
            number_format = _DATE_FORMAT if column_index in date_columns else _MONEY_FORMAT
            for cell in worksheet[letter][1:]:
                cell.number_format = number_format
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(fill_type="solid", fgColor="0758CD")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for column in worksheet.columns:
            values = [len(str(cell.value or "")) for cell in column]
            width = min(40, max(12, max(values, default=0) + 2))
            worksheet.column_dimensions[get_column_letter(column[0].column)].width = width
    workbook.save(path)
    return path


def _sheet_title(title: str) -> str:
    """Excel sheet names: at most 31 characters and none of []:*?/\\."""
    clean = "".join(" " if char in '[]:*?/\\' else char for char in title)
    return clean[:31] or "Sheet"
