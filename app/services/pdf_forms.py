"""Arabic commitment-form and payment-receipt PDFs rendered with Qt."""

from __future__ import annotations

import html
from datetime import date
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QMarginsF
from PySide6.QtGui import QFont, QFontDatabase, QPageLayout, QPageSize, QPdfWriter
from PySide6.QtGui import QTextDocument

from app.config import RESOURCE_DIR
from app.db.models import Customer, Installment, Payment, Sale
from app.i18n import ar
from app.services import schedule


def generate_commitment_pdf(
    path: str | Path,
    customer: Customer,
    sale: Sale,
    *,
    include_sensitive: bool = True,
    logo_path: str | Path | None = None,
) -> Path:
    """Create an A4 commitment form with customer data and twelve schedule rows.

    Stored installment rows are authoritative. Missing rows are filled from the
    standard first-of-month schedule. Owner-only wholesale and profit amounts are
    omitted when ``include_sensitive=False``.
    """
    if sale.sale_type != "installment":
        raise ValueError("Commitment forms are available for installment sales only")
    if sale.months is None or sale.months < 1 or sale.months > 12:
        raise ValueError("Commitment forms support installment plans from 1 to 12 months")

    schedule_rows = _commitment_schedule(sale)
    logo_uri = _logo_uri(logo_path)
    output_path = _pdf_path(path)
    body = _commitment_html(
        customer,
        sale,
        schedule_rows,
        logo_uri,
        include_sensitive,
        _commitment_due_note(sale, schedule_rows),
    )
    _write_pdf(output_path, body, ar.PDF_COMMITMENT_TITLE, QPageSize.PageSizeId.A4)
    return output_path


def generate_payment_receipt_pdf(
    path: str | Path,
    customer: Customer,
    sale: Sale,
    payment: Payment,
    *,
    issued_by: str | None = None,
    logo_path: str | Path | None = None,
) -> Path:
    """Create a small receipt for one payment present in the sale's loaded history."""
    payment_details = _receipt_payment_details(sale, payment)
    output_path = _pdf_path(path)
    body = _receipt_html(
        customer,
        sale,
        payment,
        payment_details,
        _logo_uri(logo_path),
        issued_by,
    )
    _write_pdf(output_path, body, ar.PDF_RECEIPT_TITLE, QPageSize.PageSizeId.A5)
    return output_path


def generate_cash_sale_receipt_pdf(
    path: str | Path,
    customer_name: str,
    customer_phone: str | None,
    product: str,
    sale_type: str,
    total: int,
    amount_paid: int,
    remaining_balance: int,
    purchase_date: date,
    expected_pay_date: date | None = None,
    issued_by: str | None = None,
    logo_path: str | Path | None = None,
) -> Path:
    """Create a printed receipt for spot cash or credit sales."""
    output_path = _pdf_path(path)
    body = _cash_receipt_html(
        customer_name=customer_name,
        customer_phone=customer_phone,
        product=product,
        sale_type=sale_type,
        total=total,
        amount_paid=amount_paid,
        remaining_balance=remaining_balance,
        purchase_date=purchase_date,
        expected_pay_date=expected_pay_date,
        logo_uri=_logo_uri(logo_path),
        issued_by=issued_by,
    )
    _write_pdf(output_path, body, ar.CASH_RECEIPT_TITLE, QPageSize.PageSizeId.A5)
    return output_path


def _cash_receipt_html(
    customer_name: str,
    customer_phone: str | None,
    product: str,
    sale_type: str,
    total: int,
    amount_paid: int,
    remaining_balance: int,
    purchase_date: date,
    expected_pay_date: date | None,
    logo_uri: str,
    issued_by: str | None,
) -> str:
    status_label = "خالص بالكامل نقداً" if remaining_balance <= 0 else "دفع جزئي / دين متبقي"
    fields: list[tuple[str, str]] = [
        ("تاريخ العملية", _date(purchase_date)),
        (ar.PDF_CUSTOMER_NAME, customer_name),
        (ar.PDF_PHONE, _missing(customer_phone)),
        (ar.PDF_PRODUCT, product),
        ("نوع المعاملة", "بيع نقدي (كاش)" if sale_type == "cash" else "بيع بالدين (كريدي)"),
        (ar.CASH_TOTAL_INVOICE, _money(total)),
        (ar.CASH_AMOUNT_PAID, _money(amount_paid)),
        (ar.CASH_REMAINING_BALANCE, _money(remaining_balance)),
        ("حالة الدفع", status_label),
    ]
    if expected_pay_date is not None and remaining_balance > 0:
        fields.append((ar.CASH_EXPECTED_PAY_DATE, _date(expected_pay_date)))
    if issued_by:
        fields.append((ar.PDF_RECEIVED_BY, issued_by))

    rows = "".join(
        f"<tr><td class='label'>{_escape(label)}</td><td>{_escape(value)}</td></tr>"
        for label, value in fields
    )
    return f"""<!doctype html>
<html dir="rtl"><head><meta charset="utf-8"><style>
@page {{ size: A5; margin: 12mm; }}
body {{ direction: rtl; font-family: 'Cairo', 'Noto Sans Arabic', sans-serif;
       color: #151515; font-size: 8.5pt; margin: 0; }}
table {{ border-collapse: collapse; width: 100%; }}
.header {{ border-bottom: 2px solid #333; margin-bottom: 8px; }}
.header td {{ border: 0; padding: 2px; vertical-align: middle; }}
.header img {{ width: 45px; height: 45px; }}
h1 {{ font-size: 14pt; text-align: center; margin: 0 0 2px; color: #111; }}
.shop {{ font-size: 8pt; text-align: center; color: #444; }}
.details td {{ border: 1px solid #777; padding: 3px 5px; }}
.details .label {{ width: 44%; background: #f2f2f2; font-weight: bold; }}
.signature {{ margin-top: 14px; text-align: center; height: 28px; vertical-align: bottom; }}
.signature div {{ border-top: 1px solid #aaa; margin: 0 25px 5px; }}
</style></head><body>
<table class="header"><tr><td width="25%"><img src="{_escape(logo_uri)}" width="45" height="45"></td>
<td><h1>{_escape(ar.CASH_RECEIPT_TITLE)}</h1>
<div class="shop">{_escape(ar.PDF_SHOP_NAME)} · {_escape(ar.PDF_SHOP_LOCATION)} · {_escape(ar.PDF_SHOP_OWNER)}</div></td></tr></table>
<table class="details">{rows}</table>
<div class="signature"><div></div>{_escape(ar.PDF_RECEIVER_SIGNATURE)}</div>
</body></html>"""


def _commitment_schedule(sale: Sale) -> dict[int, tuple[date, int]]:
    """Return installment due date and amount keyed by one-based row number."""
    fallback = {
        draft.installment_index: (draft.due_date, draft.amount_due)
        for draft in schedule.build(sale, {"due_mode": "first_of_month"})
    }
    stored: dict[int, tuple[date, int]] = {}
    for installment in sale.installments:
        stored[installment.installment_index] = (installment.due_date, installment.amount_due)
    fallback.update(stored)
    return fallback


def _commitment_html(
    customer: Customer,
    sale: Sale,
    schedule_rows: dict[int, tuple[date, int]],
    logo_uri: str,
    include_sensitive: bool,
    due_note: str,
) -> str:
    """Build escaped RTL HTML for a single-page A4 commitment form."""
    issue = f"{ar.PDF_ID_NUMBER}: {_missing(customer.id_number)}"
    if customer.id_issue_date is not None:
        issue += f" / {ar.PDF_ISSUED_ON}: {_date(customer.id_issue_date)}"
    if customer.id_issue_place:
        issue += f" / {ar.PDF_ISSUED_AT}: {customer.id_issue_place}"

    details: list[tuple[str, str, str, str]] = [
        (ar.PDF_CUSTOMER_NAME, customer.full_name, ar.PDF_PHONE, _missing(customer.phone)),
        (ar.PDF_DELIVERY_DATE, _date(sale.purchase_date), ar.PDF_CHEQUES_COUNT,
         _missing(customer.cheques_count)),
        (ar.PDF_ID_HOLDER, issue, ar.PDF_RESIDENCE, _missing(customer.address)),
        (ar.PDF_PROFESSION, _missing(customer.profession), ar.PDF_CCP_ACCOUNT,
         _missing(customer.ccp_number)),
    ]
    sale_details: list[tuple[str, str, str, str]] = [
        (ar.PDF_PRODUCT, sale.product, ar.PDF_SALE_TYPE, _sale_type(sale.sale_type)),
        (ar.PDF_CASH_PRICE, _money(sale.cash_price), ar.PDF_RATE, f"{sale.rate}%"),
        (ar.PDF_TOTAL_PRICE, _money(sale.total), ar.PDF_DOWN_PAYMENT, _money(sale.down_payment)),
        (ar.PDF_FINANCED_AMOUNT, _money(sale.financed), ar.PDF_MONTHLY_AMOUNT,
         _money(sale.monthly_amount)),
        (ar.PDF_MONTHS, str(sale.months), ar.PDF_END_DATE, _date(sale.end_date)),
    ]
    if include_sensitive:
        sale_details.insert(1, (ar.PDF_WHOLESALE_PRICE, _money(sale.wholesale_price),
                                ar.PDF_PROFIT, _money(sale.profit)))

    details_html = "".join(_paired_row(row) for row in details)
    sale_html = "".join(_paired_row(row) for row in sale_details)
    schedule_html: list[str] = []
    for index in range(1, 13):
        row = schedule_rows.get(index)
        if row is None:
            number, month_text, amount_text = str(index), "&nbsp;", "&nbsp;"
            row_class = "unused"
        else:
            due_date, amount = row
            number = str(index)
            month_text = _date(due_date)
            amount_text = _money(amount)
            row_class = ""
        if row_class:
            cells = (number, month_text, amount_text)
            rendered = tuple(f"<s>{value}</s>" for value in cells)
        else:
            rendered = tuple(_escape(value) for value in (number, month_text, amount_text))
        schedule_html.append(
            f'<tr class="{row_class}"><td>{rendered[0]}</td>'
            f"<td>{rendered[1]}</td><td>{rendered[2]}</td></tr>"
        )

    return f"""<!doctype html>
<html dir="rtl"><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 10mm; }}
body {{ direction: rtl; font-family: 'Cairo', 'Noto Sans Arabic', sans-serif;
       color: #151515; font-size: 7.5pt; margin: 0; }}
table {{ border-collapse: collapse; width: 100%; }}
.brand {{ border-bottom: 2px solid #333; margin-bottom: 2px; }}
.brand td {{ border: 0; padding: 2px 4px; vertical-align: middle; }}
.brand img {{ width: 64px; height: 64px; }}
h1 {{ font-size: 16pt; text-align: center; margin: 1px 0 2px; }}
.shop {{ font-size: 8pt; line-height: 1.2; text-align: center; }}
.section-title {{ font-size: 10pt; font-weight: bold; text-align: center;
                  border: 1px solid #555; background: #eee; padding: 2px; margin-top: 3px; }}
.details td {{ border: 1px solid #777; padding: 2px 4px; }}
.details .label {{ width: 16%; font-weight: bold; background: #f2f2f2; }}
.details .value {{ width: 34%; min-height: 17px; }}
.statement {{ margin: 3px 1px 2px; font-weight: bold; }}
.schedule {{ font-size: 7.5pt; }}
.schedule th, .schedule td {{ border: 1px solid #555; text-align: center; padding: 0 1px; height: 11px; }}
.schedule th {{ background: #eee; }}
.schedule .unused {{ color: #888; }}
.note {{ border: 1px solid #777; padding: 2px 4px; margin-top: 3px; text-align: center; font-size: 7pt; }}
.signatures {{ margin-top: 6px; font-size: 7pt; }}
.signatures td {{ border: 1px solid #666; height: 38px; width: 33.33%;
                   vertical-align: bottom; text-align: center; padding: 3px; }}
.signatures .line {{ border-top: 1px solid #aaa; margin: 0 4px 4px; }}
</style></head><body>
<table class="brand"><tr>
<td width="25%"><img src="{_escape(logo_uri)}" width="64" height="64" /></td>
<td width="75%"><h1>{_escape(ar.PDF_COMMITMENT_TITLE)}</h1>
<div class="shop"><b>{_escape(ar.PDF_SHOP_NAME)} — {_escape(ar.PDF_SHOP_LOCATION)}</b><br>
{_escape(ar.PDF_SHOP_ACTIVITY)}<br>{_escape(ar.PDF_SHOP_OWNER)}</div></td>
</tr></table>
<table class="details">{details_html}</table>
<div class="section-title">{_escape(ar.PDF_PRODUCT)}: {_escape(sale.product)}</div>
<table class="details">{sale_html}</table>
<div class="statement">{_escape(ar.PDF_SIGNER_STATEMENT)}</div>
<div class="section-title">{_escape(ar.PDF_SCHEDULE_TITLE)}</div>
<table class="schedule"><thead><tr><th>{_escape(ar.PDF_COL_NUMBER)}</th>
<th>{_escape(ar.PDF_COL_MONTH)}</th><th>{_escape(ar.PDF_COL_MONTHLY_PAYMENT)}</th></tr></thead>
<tbody>{''.join(schedule_html)}</tbody></table>
<div class="note">{_escape(due_note)}</div>
<table class="signatures"><tr>
<td><div class="line"></div>{_escape(ar.PDF_SIGNATURE_CUSTOMER)}</td>
<td><div class="line"></div>{_escape(ar.PDF_SIGNATURE_MUNICIPALITY)}</td>
<td><div class="line"></div>{_escape(ar.PDF_SIGNATURE_SELLER)}</td>
</tr></table>
</body></html>"""


def _commitment_due_note(
    sale: Sale,
    schedule_rows: dict[int, tuple[date, int]],
) -> str:
    """Describe the actual saved due-date pattern on the printed contract."""
    due_days = {due_date.day for due_date, _amount in schedule_rows.values()}
    if due_days == {1}:
        return ar.PDF_NOTE_FIRST_OF_MONTH
    if due_days == {sale.purchase_date.day}:
        return ar.PDF_NOTE_PURCHASE_DAY.format(day=sale.purchase_date.day)
    return ar.PDF_NOTE_SCHEDULE


def _receipt_payment_details(sale: Sale, payment: Payment) -> dict[str, object]:
    """Verify payment membership and calculate the balance after that history row."""
    if sale.sale_type == "credit":
        if payment.sale_id != sale.id:
            raise ValueError("Payment does not belong to this credit sale")
        history = sorted(sale.credit_payments, key=lambda item: item.id)
        if not any(item.id == payment.id for item in history):
            raise ValueError("Payment is not present in the loaded credit history")
        paid_through = sum(item.amount for item in history if item.id <= payment.id)
        return {
            "installment": None,
            "balance_after": max(0, sale.financed - paid_through),
            "amount_due": None,
        }

    installment = next(
        (row for row in sale.installments if row.id == payment.installment_id), None
    )
    if installment is None:
        raise ValueError("Payment installment is not present in the loaded sale history")
    history = sorted(installment.payments, key=lambda item: item.id)
    if not any(item.id == payment.id for item in history):
        raise ValueError("Payment is not present in the loaded installment history")
    recorded_total = sum(item.amount for item in history)
    legacy_paid = max(0, installment.amount_paid - recorded_total)
    paid_through = sum(item.amount for item in history if item.id <= payment.id)
    return {
        "installment": installment,
        "balance_after": max(0, installment.amount_due - legacy_paid - paid_through),
    }


def _receipt_html(
    customer: Customer,
    sale: Sale,
    payment: Payment,
    payment_details: dict[str, object],
    logo_uri: str,
    issued_by: str | None,
) -> str:
    """Build concise RTL receipt HTML for one installment or credit payment."""
    installment = payment_details["installment"]
    fields: list[tuple[str, str]] = [
        (ar.PDF_RECEIPT_NUMBER, str(payment.id)),
        (ar.PDF_RECEIPT_DATE, _date(date.today())),
        (ar.PDF_CUSTOMER_NAME, customer.full_name),
        (ar.PDF_PHONE, _missing(customer.phone)),
        (ar.PDF_PRODUCT, sale.product),
        (ar.PDF_SALE_TYPE, _sale_type(sale.sale_type)),
        (ar.PDF_PAID_AMOUNT, _money(payment.amount)),
        (ar.PDF_PAYMENT_DATE, _date(payment.payment_date)),
        (ar.PDF_PAYMENT_METHOD, _payment_method(payment.method)),
    ]
    if isinstance(installment, Installment):
        fields.extend(
            (
                (ar.PDF_INSTALLMENT_NUMBER, str(installment.installment_index)),
                (ar.PDF_INSTALLMENT_DUE, _date(installment.due_date)),
                (ar.PDF_INSTALLMENT_AMOUNT, _money(installment.amount_due)),
            )
        )
    fields.append((ar.PDF_REMAINING_BALANCE, _money(int(payment_details["balance_after"]))))
    if payment.note:
        fields.append((ar.PDF_PAYMENT_NOTE, payment.note))
    if issued_by:
        fields.append((ar.PDF_RECEIVED_BY, issued_by))

    rows = "".join(
        f"<tr><td class='label'>{_escape(label)}</td><td>{_escape(value)}</td></tr>"
        for label, value in fields
    )
    return f"""<!doctype html>
<html dir="rtl"><head><meta charset="utf-8"><style>
@page {{ size: A5; margin: 12mm; }}
body {{ direction: rtl; font-family: 'Cairo', 'Noto Sans Arabic', sans-serif;
       color: #151515; font-size: 8pt; margin: 0; }}
table {{ border-collapse: collapse; width: 100%; }}
.header {{ border-bottom: 2px solid #333; margin-bottom: 5px; }}
.header td {{ border: 0; padding: 2px; vertical-align: middle; }}
.header img {{ width: 45px; height: 45px; }}
h1 {{ font-size: 14pt; text-align: center; margin: 0 0 2px; }}
.shop {{ font-size: 7.5pt; text-align: center; }}
.details td {{ border: 1px solid #777; padding: 2px 3px; }}
.details .label {{ width: 42%; background: #f2f2f2; font-weight: bold; }}
.thank-you {{ margin-top: 7px; text-align: center; font-size: 8pt; }}
.signature {{ margin-top: 9px; text-align: center; height: 28px; vertical-align: bottom; }}
.signature div {{ border-top: 1px solid #aaa; margin: 0 25px 5px; }}
</style></head><body>
<table class="header"><tr><td width="25%"><img src="{_escape(logo_uri)}" width="45" height="45"></td>
<td><h1>{_escape(ar.PDF_RECEIPT_TITLE)}</h1>
<div class="shop">{_escape(ar.PDF_SHOP_NAME)} · {_escape(ar.PDF_SHOP_LOCATION)} · {_escape(ar.PDF_SHOP_OWNER)}</div></td></tr></table>
<table class="details">{rows}</table>
<div class="signature"><div></div>{_escape(ar.PDF_RECEIVER_SIGNATURE)}</div>
</body></html>"""


def _write_pdf(path: Path, content: str, title: str, page_size: QPageSize.PageSizeId) -> None:
    """Render HTML using Qt's document engine and a PDF writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(path))
    writer.setTitle(title)
    writer.setCreator("AkremMobile")
    writer.setResolution(300)
    writer.setPageSize(QPageSize(page_size))
    writer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Millimeter)
    document = QTextDocument()
    document.setDefaultFont(QFont(_cairo_font_family(), 9))
    document.setHtml(content)
    document.setPageSize(writer.pageLayout().paintRect(QPageLayout.Unit.Point).size())
    document.print_(writer)
    del document
    del writer
    if not path.is_file() or path.stat().st_size == 0:
        raise OSError(f"Qt did not create the requested PDF: {path}")


def _logo_uri(path: str | Path | None) -> str:
    """Return a local URI for the bundled logo or a caller-provided logo asset."""
    logo = Path(path) if path is not None else RESOURCE_DIR / "logo.png"
    if not logo.is_file():
        raise FileNotFoundError(f"AkremMobile logo not found: {logo}")
    return logo.resolve().as_uri()


@lru_cache(maxsize=1)
def _cairo_font_family() -> str:
    """Register and return the bundled Cairo family for portable PDF output."""
    font_path = RESOURCE_DIR / "fonts" / "Cairo-Variable.ttf"
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    if font_id >= 0:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            return families[0]
    return "Cairo"


def _pdf_path(path: str | Path) -> Path:
    """Resolve an output path and append ``.pdf`` when it has no PDF suffix."""
    output = Path(path).expanduser()
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    return output.resolve()


def _paired_row(values: tuple[str, str, str, str]) -> str:
    """Render two label/value pairs for a compact printed form."""
    left_label, left_value, right_label, right_value = values
    return (
        f'<tr><td class="label">{_escape(left_label)}</td><td class="value">{_escape(left_value)}</td>'
        f'<td class="label">{_escape(right_label)}</td><td class="value">{_escape(right_value)}</td></tr>'
    )


def _sale_type(sale_type: str) -> str:
    """Return an Arabic PDF label for the sale type."""
    return {
        "cash": ar.PDF_SALE_CASH,
        "installment": ar.PDF_SALE_INSTALLMENT,
        "credit": ar.PDF_SALE_CREDIT,
    }.get(sale_type, sale_type)


def _payment_method(method: str) -> str:
    """Translate a supported stored payment method for printed receipts."""
    return {
        "cash": ar.PDF_METHOD_CASH,
        "ccp": ar.PDF_METHOD_CCP,
        "baridimob": ar.PDF_METHOD_BARIDIMOB,
    }.get(method.casefold(), method)


def _money(value: int | None) -> str:
    """Format whole dinars for RTL output."""
    return ar.PDF_VALUE_MISSING if value is None else f"{value:,} {ar.CURRENCY_SUFFIX}"


def _date(value: date | None) -> str:
    """Format dates using the application's DD/MM/YYYY convention."""
    return ar.PDF_VALUE_MISSING if value is None else value.strftime("%d/%m/%Y")


def _missing(value: object) -> str:
    """Display an optional text/count field without Python's None spelling."""
    return ar.PDF_VALUE_MISSING if value is None or value == "" else str(value)


def _escape(value: object) -> str:
    """HTML-escape all dynamic form data and localized labels."""
    return html.escape(str(value), quote=True)
