"""Preview and import the shop's legacy Excel installment sheet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping
import unicodedata

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category, Customer, Installment, Payment, Sale
from app.services import calc, customers, sales
from app.services.calc import SaleCalculation
from app.services.customers import normalize_search_text
from app.services.payments import lock_payment_ledger

SHEET_NAME = "التقسيط"
HEADERS = {
    "full_name": "الاسم واللقب",
    "product": "المنتج",
    "sale_type": "التصنيف",
    "wholesale_price": "سعر الجملة",
    "cash_price": "سعر الكاش",
    "rate": "نسبة التقسيط",
    "financed": "سعر البيع",
    "down_payment": "الدفعة الأولية",
    "months": "عدد أشهر التقسيط",
    "monthly_amount": "الاقتطاع الشهري",
    "profit": "الفائدة",
    "purchase_date": "تاريخ شراء المنتج",
    "end_date": "تاريخ انتهاء الاقتطاع",
}
REQUIRED_HEADERS = (
    "full_name", "product", "sale_type", "wholesale_price", "cash_price",
    "rate", "financed", "down_payment", "months", "purchase_date",
)
_SALE_TYPES = {
    "cash": "cash", "كاش": "cash", "نقدا": "cash", "نقداً": "cash",
    "installment": "installment", "بالتقسيط": "installment", "تقسيط": "installment",
    "credit": "credit", "كريدي": "credit", "كريديت": "credit",
}


@dataclass(frozen=True, slots=True)
class ImportRow:
    """One validated source row with calculated values and preview diagnostics."""

    source_row: int
    full_name: str
    product: str
    sale_type: str | None
    wholesale_price: int | None
    cash_price: int | None
    rate: int | None
    down_payment: int | None
    months: int | None
    purchase_date: date | None
    sheet_financed: int | None
    sheet_monthly: int | None
    sheet_profit: int | None
    calculation: SaleCalculation | None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        """Whether the row can be imported without further correction."""
        return not self.errors and self.sale_type is not None and self.calculation is not None


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """Parsed workbook preview and header-to-column mapping."""

    path: Path
    sheet_name: str
    headers: tuple[str, ...]
    mapping: Mapping[str, int]
    rows: tuple[ImportRow, ...]
    missing_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Summary returned after the caller commits or rolls back the transaction."""

    imported: int
    skipped: int
    warnings: int
    sale_ids: tuple[int, ...]
    duplicate_rows: tuple[int, ...] = ()


class WorkbookImportError(ValueError):
    """Raised when a workbook cannot be opened or its requested sheet is absent."""


def preview_workbook(
    path: str | Path,
    *,
    sheet_name: str = SHEET_NAME,
    mapping: Mapping[str, int] | None = None,
    sale_type_overrides: Mapping[int, str] | None = None,
) -> ImportPreview:
    """Read a worksheet's displayed/formula-result values and build a row preview.

    ``mapping`` maps internal field names to zero-based column positions and can
    be supplied by a column-mapping UI when expected headers are missing.
    ``sale_type_overrides`` maps Excel row numbers to cash/installment/credit.
    """
    source = Path(path)
    suffix = source.suffix.casefold()
    if suffix not in {".xlsx", ".xlsm", ".xls"}:
        raise WorkbookImportError("Choose an Excel .xlsx, .xlsm, or .xls file")
    if suffix == ".xls":
        matrix, chosen_sheet = _read_xls(source, sheet_name)
    else:
        matrix, chosen_sheet = _read_openpyxl(source, sheet_name)
    if not matrix:
        raise WorkbookImportError("The selected worksheet is empty")

    headers = tuple(_clean_text(value) for value in matrix[0])
    resolved_mapping = dict(_detect_mapping(headers) if mapping is None else mapping)
    for field, column in resolved_mapping.items():
        if field not in HEADERS:
            raise WorkbookImportError(f"Unknown mapped field: {field}")
        if isinstance(column, bool) or not isinstance(column, int) or not 0 <= column < len(headers):
            raise WorkbookImportError(f"Invalid worksheet column mapping for {field}")
    missing = tuple(field for field in REQUIRED_HEADERS if field not in resolved_mapping)
    overrides = sale_type_overrides or {}
    rows: list[ImportRow] = []
    for excel_row, raw in enumerate(matrix[1:], start=2):
        if all(_is_blank(value) for value in raw):
            continue
        values = {
            field: raw[column] if column < len(raw) else None
            for field, column in resolved_mapping.items()
        }
        rows.append(_parse_row(excel_row, values, overrides.get(excel_row)))
    return ImportPreview(source, chosen_sheet, headers, resolved_mapping, tuple(rows), missing)


def import_preview(
    session: Session,
    preview: ImportPreview,
    *,
    uncategorized_id: int,
    mark_past_due_paid: bool = True,
    use_sheet_values: set[int] | None = None,
) -> ImportResult:
    """Import valid, nonduplicate rows in the caller's transaction.

    Customers with the same normalized name are merged. Missing spreadsheet
    phones stay empty and all new customers use the selected uncategorized group.
    Earlier installment rows are recorded as cash payments when requested.
    """
    if preview.missing_columns:
        raise ValueError("Column mapping is incomplete")
    lock_payment_ledger(session)
    category = session.get(Category, uncategorized_id)
    if category is None:
        raise ValueError("Uncategorized customer category not found")

    customers_by_name = {
        normalize_search_text(row.full_name): row
        for row in session.scalars(select(Customer).order_by(Customer.id))
    }
    imported_ids: list[int] = []
    duplicate_rows: list[int] = []
    skipped = 0
    warning_count = 0
    source_value_rows = use_sheet_values or set()
    for row in preview.rows:
        if not row.ready:
            skipped += 1
            continue
        assert row.calculation is not None
        key = normalize_search_text(row.full_name)
        customer = customers_by_name.get(key)
        if customer is None:
            customer = customers.create_customer(
                session,
                full_name=row.full_name,
                phone=None,
                category_id=uncategorized_id,
            )
            customers_by_name[key] = customer

        duplicate = session.scalar(
            select(Sale.id).where(
                Sale.customer_id == customer.id,
                Sale.product == row.product,
                Sale.purchase_date == row.purchase_date,
                Sale.cash_price == row.cash_price,
            ).limit(1)
        )
        if duplicate is not None:
            skipped += 1
            duplicate_rows.append(row.source_row)
            continue

        sale = sales.create_sale(
            session,
            customer_id=customer.id,
            product=row.product,
            sale_type=row.sale_type or "",
            wholesale_price=row.wholesale_price if row.wholesale_price is not None else -1,
            cash_price=row.cash_price if row.cash_price is not None else -1,
            rate=(row.rate or 0) if row.sale_type != "cash" else 0,
            down_payment=(row.down_payment or 0) if row.sale_type != "cash" else 0,
            months=row.months if row.sale_type == "installment" else None,
            purchase_date=row.purchase_date,
        )
        if row.source_row in source_value_rows:
            _apply_sheet_values(sale, row)
        imported_ids.append(sale.id)
        warning_count += len(row.warnings)
        if mark_past_due_paid:
            for installment in sale.installments:
                if installment.due_date < date.today():
                    payment = Payment(
                        installment_id=installment.id,
                        amount=installment.amount_due,
                        payment_date=installment.due_date,
                        method="cash",
                        note="Imported as paid from the previous Excel sheet",
                    )
                    session.add(payment)
                    installment.amount_paid = installment.amount_due
                    installment.paid_date = installment.due_date
                    installment.method = "cash"
        session.flush()
    return ImportResult(
        len(imported_ids),
        skipped,
        warning_count,
        tuple(imported_ids),
        tuple(duplicate_rows),
    )


def _apply_sheet_values(sale: Sale, row: ImportRow) -> None:
    """Keep selected legacy totals while ensuring the generated schedule balances."""
    target_financed = row.sheet_financed if row.sheet_financed is not None else sale.financed
    if target_financed < 0:
        raise ValueError(f"Spreadsheet row {row.source_row} has a negative financed amount")
    sale.financed = target_financed
    sale.total = target_financed + sale.down_payment
    sale.profit = (
        row.sheet_profit
        if row.sheet_profit is not None
        else sale.total - sale.wholesale_price
    )
    if sale.sale_type != "installment":
        sale.monthly_amount = None
        return

    installments = sorted(sale.installments, key=lambda item: item.installment_index)
    if not installments:
        return
    months = len(installments)
    monthly_base = row.sheet_monthly
    if monthly_base is not None and monthly_base * (months - 1) <= target_financed:
        amounts = [monthly_base] * (months - 1)
        amounts.append(target_financed - sum(amounts))
    else:
        base, remainder = divmod(target_financed, months)
        amounts = [base] * months
        amounts[-1] += remainder
    for installment, amount in zip(installments, amounts, strict=True):
        installment.amount_due = amount
    sale.monthly_amount = amounts[0]


def _read_openpyxl(path: Path, sheet_name: str) -> tuple[list[list[Any]], str]:
    """Read cached formula results from modern Excel workbooks."""
    try:
        workbook = load_workbook(path, data_only=True, read_only=True)
    except Exception as error:
        raise WorkbookImportError("Could not open this Excel workbook") from error
    try:
        if sheet_name not in workbook.sheetnames:
            raise WorkbookImportError(f"Worksheet {sheet_name!r} was not found")
        chosen = sheet_name
        worksheet = workbook[chosen]
        return [list(row) for row in worksheet.iter_rows(values_only=True)], chosen
    finally:
        workbook.close()


def _read_xls(path: Path, sheet_name: str) -> tuple[list[list[Any]], str]:
    """Read legacy binary Excel files through pandas' xlrd engine."""
    try:
        import pandas as pd

        book = pd.ExcelFile(path, engine="xlrd")
        if sheet_name not in book.sheet_names:
            raise WorkbookImportError(f"Worksheet {sheet_name!r} was not found")
        chosen = sheet_name
        frame = pd.read_excel(book, sheet_name=chosen, header=None, dtype=object)
        matrix = frame.where(frame.notna(), None).values.tolist()
        return matrix, chosen
    except WorkbookImportError:
        raise
    except Exception as error:
        raise WorkbookImportError("Could not read this .xls workbook; confirm it is a valid Excel file") from error
    finally:
        if "book" in locals():
            book.close()


def _detect_mapping(headers: tuple[str, ...]) -> dict[str, int]:
    """Match trimmed Arabic headers, tolerating Arabic spelling variants."""
    normalized = {_normalize_header(label): index for index, label in enumerate(headers)}
    mapping: dict[str, int] = {}
    for field, expected in HEADERS.items():
        match = normalized.get(_normalize_header(expected))
        if match is not None:
            mapping[field] = match
    return mapping


def _parse_row(excel_row: int, values: Mapping[str, Any], override: str | None) -> ImportRow:
    """Parse inputs, calculate the expected result, and collect discrepancies."""
    name = _clean_text(values.get("full_name"))
    product = _clean_text(values.get("product"))
    errors: list[str] = []
    warnings: list[str] = []
    if not name:
        errors.append("اسم الزبون مفقود")
    elif normalize_search_text(name) in {"x", "xx", "xxx", "غير معروف"}:
        warnings.append("اسم مؤقت؛ راجعه قبل الاستيراد")
    if not product:
        errors.append("اسم المنتج مفقود")

    raw_type = _clean_text(override if override is not None else values.get("sale_type"))
    sale_type = _SALE_TYPES.get(_normalize_header(raw_type)) if raw_type else None
    if raw_type and sale_type is None:
        errors.append("نوع البيع غير معروف")
    elif not raw_type:
        warnings.append("اختر نوع البيع قبل الاستيراد")

    wholesale = _parse_integer(values.get("wholesale_price"), "سعر الجملة", errors)
    cash = _parse_integer(values.get("cash_price"), "سعر الكاش", errors)
    rate = _parse_integer(values.get("rate"), "نسبة التقسيط", errors, optional=True) or 0
    down = _parse_integer(values.get("down_payment"), "الدفعة الأولية", errors, optional=True) or 0
    months = _parse_integer(values.get("months"), "عدد الأشهر", errors, optional=True)
    purchase_date = _parse_date(values.get("purchase_date"), errors)
    source_value_errors: list[str] = []
    sheet_financed = _parse_integer(values.get("financed"), "سعر البيع", source_value_errors, optional=True)
    sheet_monthly = _parse_integer(values.get("monthly_amount"), "الاقتطاع الشهري", source_value_errors, optional=True)
    sheet_profit = _parse_integer(values.get("profit"), "الفائدة", source_value_errors, optional=True)
    warnings.extend(source_value_errors)

    calculation = None
    if not errors and sale_type is not None:
        resolved_months = months if sale_type == "installment" else None
        resolved_rate = rate if sale_type == "installment" else 0
        resolved_down = down if sale_type != "cash" else 0
        try:
            calculation = calc.compute_sale(
                cash_price=cash,
                rate=resolved_rate,
                down_payment=resolved_down,
                months=resolved_months,
                wholesale=wholesale,
                sale_type=sale_type,
                purchase_date=purchase_date,
            )
        except (TypeError, ValueError):
            errors.append("تعذر حساب البيع؛ تحقق من المبلغ والمدة والدفعة الأولية")
    if calculation is not None:
        if sheet_financed is not None and sheet_financed != calculation.financed:
            warnings.append("سعر البيع في الملف يختلف عن الحساب")
        if sheet_monthly is not None and calculation.monthly_list and sheet_monthly != calculation.monthly_list[0]:
            warnings.append("الاقتطاع الشهري في الملف يختلف عن الحساب")
        if sheet_profit is not None and sheet_profit != calculation.profit:
            warnings.append("الفائدة في الملف تختلف عن الحساب")
    warnings.append("رقم الهاتف غير موجود في ملف Excel")
    return ImportRow(
        excel_row, name, product, sale_type, wholesale, cash, rate, down,
        months, purchase_date, sheet_financed, sheet_monthly, sheet_profit,
        calculation, tuple(dict.fromkeys(warnings)), tuple(dict.fromkeys(errors)),
    )


def _parse_integer(value: Any, label: str, errors: list[str], *, optional: bool = False) -> int | None:
    """Convert whole-dinar spreadsheet values to nearest integer dinar."""
    if _is_blank(value):
        if optional:
            return None
        errors.append(f"{label} مفقود")
        return None
    if isinstance(value, bool):
        errors.append(f"{label} غير صالح")
        return None
    try:
        text = _western_digits(str(value)).replace("دج", "").replace("DA", "").replace("da", "")
        text = text.replace(" ", "").replace("٬", "").replace(",", "")
        number = Decimal(text).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        result = int(number)
        if result < 0:
            errors.append(f"{label} غير صالح")
            return None
        return result
    except (InvalidOperation, ValueError, TypeError):
        errors.append(f"{label} غير صالح")
        return None


def _parse_date(value: Any, errors: list[str]) -> date | None:
    """Parse a native Excel date or common localized date string."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if _is_blank(value):
        errors.append("تاريخ الشراء مفقود")
        return None
    text = _western_digits(str(value).strip())
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    if isinstance(value, (int, float)):
        try:
            return date(1899, 12, 30) + timedelta(days=int(value))
        except (OverflowError, ValueError):
            pass
    errors.append("تاريخ الشراء غير صالح")
    return None


def _normalize_header(value: str) -> str:
    """Normalize an Arabic Excel header for matching and sale type parsing."""
    result: list[str] = []
    for char in unicodedata.normalize("NFKD", value).casefold():
        if unicodedata.category(char).startswith("M") or char == "ـ":
            continue
        if char in "أإآٱء":
            result.append("ا")
        elif char == "ة":
            result.append("ه")
        else:
            result.append(char)
    return "".join(result).strip()


def _western_digits(value: str) -> str:
    """Translate Arabic and Persian decimal digits to Western digits."""
    return "".join(str(unicodedata.digit(char)) if char.isdecimal() else char for char in value)


def _clean_text(value: Any) -> str:
    """Return stripped display text or an empty string for blank cells."""
    if _is_blank(value):
        return ""
    return str(value).strip()


def _is_blank(value: Any) -> bool:
    """Treat spreadsheet blanks, NaN, and whitespace-only strings uniformly."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Decimal):
        return value.is_nan()
    try:
        if value != value:
            return True
    except (TypeError, ValueError):
        pass
    return bool(getattr(value, "__class__", None) and value.__class__.__name__ == "NAType")
