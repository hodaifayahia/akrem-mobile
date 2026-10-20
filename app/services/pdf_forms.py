"""Arabic commitment-form (تعهد والتزام) and receipt PDFs rendered with Qt.

The documents are HTML laid out by ``QTextDocument`` and painted page by
page onto a ``QPdfWriter`` (or an image, for the live preview), with a page
frame and a faint logo watermark drawn behind the text. A document that is a
little too tall for one page is shrunk to fit it, so a commitment form stays
on one sheet; a much longer one flows onto more pages.

Every text the owner can change comes from ``print_templates``; Latin product
names, IMEIs and references are isolated so they keep their reading order in
right-to-left documents.
"""

from __future__ import annotations

import hashlib
import html
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QMarginsF, QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QAbstractTextDocumentLayout,
    QColor,
    QFont,
    QFontDatabase,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPainterPath,
    QPalette,
    QPdfWriter,
    QPen,
    QTextDocument,
)

from app.config import RESOURCE_DIR
from app.db.models import Customer, Installment, Payment, Sale
from app.i18n import ar, is_rtl
from app.i18n.plan_text import every_text
from app.services import calc, schedule
from app.services.print_templates import PrintTemplates, default_templates

# Print palette: dark ink on white, the brand blues for structure only.
INK = "#0F172A"
MUTED = "#64748B"
LINE = "#CBD5E1"
SOFT = "#F1F5F9"
NAVY = "#0B2A5B"
BLUE = "#0758CD"
PAID = "#15803D"
PAID_FILL = "#E8F7EE"
DUE = "#B45309"
DUE_FILL = "#FDF3E2"

#: Layout unit: 1/96 inch (a CSS pixel), whatever the screen.
_DOC_DPI = 96.0
_MIN_FIT_SCALE = 0.72
PREVIEW_KINDS = ("commitment", "cash_receipt", "payment_receipt")
_RTL_RANGES = ((0x0590, 0x08FF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))
_RLM = "‏"  # RIGHT-TO-LEFT MARK: makes a line that starts with Latin text read right to left


@dataclass(frozen=True, slots=True)
class ReceiptItem:
    """One product line of a cash/credit receipt (``amount`` is the line total)."""

    name: str
    quantity: int
    amount: int


@dataclass(frozen=True, slots=True)
class _PageSpec:
    """Paper, margins and decorations of one document kind."""

    size: QPageSize.PageSizeId
    margin_mm: float
    frame_mm: float
    watermark: bool


# ---------------------------------------------------------------- public API
def generate_commitment_pdf(
    path: str | Path,
    customer: Customer,
    sale: Sale,
    *,
    include_sensitive: bool | None = None,
    logo_path: str | Path | None = None,
    templates: PrintTemplates | None = None,
    business_nis: str | None = None,
    business_nif: str | None = None,
) -> Path:
    """Create the A4 commitment form of an installment sale (up to 12 payments).

    Stored installment rows are authoritative; missing rows are filled from
    the standard schedule. ``include_sensitive`` adds the wholesale price and
    the profit (an owner's copy); ``None`` follows the owner's print setting.
    ``business_nis``/``business_nif`` override the saved NIS and NIF.
    """
    if sale.sale_type != "installment":
        raise ValueError("Commitment forms are available for installment sales only")
    try:
        payment_count = len(calc.payment_offsets(sale.months, sale.payment_interval or 1))
    except (TypeError, ValueError) as error:
        raise ValueError("Commitment forms need a valid installment plan") from error
    if payment_count > 12:
        raise ValueError("Commitment forms support installment plans of up to 12 payments")
    templates = templates or default_templates()
    if include_sensitive is None:
        include_sensitive = templates.commitment_owner_figures
    body = commitment_html(
        customer, sale, templates, _logo_uri(logo_path), include_sensitive=include_sensitive,
        business_nis=business_nis, business_nif=business_nif,
    )
    output_path = _pdf_path(path)
    _write_pdf(output_path, body, templates.commitment_title, _commitment_page(templates), logo_path)
    return output_path


def generate_payment_receipt_pdf(
    path: str | Path,
    customer: Customer,
    sale: Sale,
    payment: Payment,
    *,
    issued_by: str | None = None,
    logo_path: str | Path | None = None,
    templates: PrintTemplates | None = None,
    business_nis: str | None = None,
    business_nif: str | None = None,
    basket: Sequence[Sale] | None = None,
) -> Path:
    """Create an A5 receipt for one payment present in the sale's loaded history.

    The receipt is for the whole amount the client handed over: every part
    of the payment's batch (the installments it was spread over, and the
    other products of ``basket``, the loaded sales bought with ``sale``).
    ``business_nis``/``business_nif`` override the saved NIS and NIF.
    """
    templates = templates or default_templates()
    details = _receipt_payment_details(sale, payment)
    details.update(_batch_receipt_details(tuple(basket) if basket else (sale,), sale, payment))
    body = _receipt_html(
        customer, sale, payment, details, _logo_uri(logo_path), issued_by, templates,
        business_nis=business_nis, business_nif=business_nif,
    )
    output_path = _pdf_path(path)
    _write_pdf(output_path, body, ar.PDF_RECEIPT_TITLE, _receipt_page(templates), logo_path)
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
    business_nis: str | None = None,
    business_nif: str | None = None,
    *,
    items: Sequence[ReceiptItem] | None = None,
    receipt_number: int | None = None,
    templates: PrintTemplates | None = None,
) -> Path:
    """Create the A5 receipt of a spot cash or credit sale (one or several products)."""
    templates = templates or default_templates()
    body = _cash_receipt_html(
        customer_name, customer_phone, product, sale_type, total, amount_paid, remaining_balance,
        purchase_date, expected_pay_date, _logo_uri(logo_path), issued_by, business_nis, business_nif,
        items=items, receipt_number=receipt_number, templates=templates,
    )
    output_path = _pdf_path(path)
    _write_pdf(output_path, body, ar.CASH_RECEIPT_TITLE, _receipt_page(templates), logo_path)
    return output_path


def preview_image(kind: str, templates: PrintTemplates, width: int) -> QImage:
    """Render page one of a sample document with ``templates``, ``width`` pixels wide."""
    body, spec, _title = _preview_document(kind, templates)
    return _render_image(body, spec, width, None)


def write_preview_pdf(kind: str, templates: PrintTemplates, path: str | Path) -> Path:
    """Write the sample document of ``kind`` as a PDF (the owner checks the real print)."""
    body, spec, title = _preview_document(kind, templates)
    output_path = _pdf_path(path)
    _write_pdf(output_path, body, title, spec)
    return output_path


def _preview_document(kind: str, templates: PrintTemplates) -> tuple[str, _PageSpec, str]:
    """HTML, page and title of a sample commitment form, sale receipt or payment receipt."""
    if kind not in PREVIEW_KINDS:
        raise ValueError(f"Unknown document kind: {kind}")
    customer, sale, payment = _sample_documents()
    logo = _logo_uri(None)
    if kind == "commitment":
        body = commitment_html(customer, sale, templates, logo,
                               include_sensitive=templates.commitment_owner_figures)
        return body, _commitment_page(templates), templates.commitment_title
    if kind == "payment_receipt":
        details = _receipt_payment_details(sale, payment)
        body = _receipt_html(customer, sale, payment, details, logo, ar.PRINT_SAMPLE_SELLER, templates)
        return body, _receipt_page(templates), ar.PDF_RECEIPT_TITLE
    items = (ReceiptItem(sale.product, 1, 45_000), ReceiptItem(ar.PRINT_SAMPLE_ACCESSORY, 2, 3_000))
    body = _cash_receipt_html(
        customer.full_name, customer.phone, "", "credit", 48_000, 30_000, 18_000, sale.purchase_date,
        None, logo, None, None, None, items=items, receipt_number=128, templates=templates,
    )
    return body, _receipt_page(templates), ar.CASH_RECEIPT_TITLE


# ----------------------------------------------------------- commitment form
def commitment_html(
    customer: Customer,
    sale: Sale,
    templates: PrintTemplates,
    logo_uri: str,
    *,
    include_sensitive: bool = False,
    business_nis: str | None = None,
    business_nif: str | None = None,
) -> str:
    """Escaped RTL HTML of the commitment form, laid out like the shop's paper form."""
    schedule_rows = _commitment_schedule(sale)
    paid = _paid_by_installment(sale)
    values = _placeholder_values(customer, sale, schedule_rows, templates)

    # The paper form's fields, three per line: a small caption over a dotted value line.
    customer_rows = [
        [(ar.PDF_CUSTOMER_NAME, _text(customer.full_name), 44), (ar.PDF_PHONE, _ltr_html(customer.phone), 28),
         (ar.PDF_PROFESSION, _text(customer.profession), 28)],
        [(ar.PDF_ID_HOLDER, _ltr_html(customer.id_number), 44),
         (ar.PDF_ISSUED_ON, _date_or_blank(customer.id_issue_date), 28),
         (ar.PDF_ISSUED_AT, _text(customer.id_issue_place), 28)],
        [(ar.PDF_RESIDENCE, _text(customer.address), 44), (ar.PDF_CCP_ACCOUNT, _ltr_html(customer.ccp_number), 28),
         (ar.PDF_CHEQUES_COUNT, _text(customer.cheques_count), 28)],
    ]
    count = len(schedule_rows)
    every = every_text(sale.payment_interval or 1)
    figures = [
        (ar.PDF_TOTAL_PRICE, _money(sale.total), False),
        (ar.PDF_DOWN_PAYMENT, _money(sale.down_payment), False),
        (ar.PDF_FINANCED_AMOUNT, _money(sale.financed), False),
        (ar.PDF_MONTHLY_AMOUNT, _money(sale.monthly_amount), True),
        (f"{ar.PRINT_PLAN} · {every}", ar.PRINT_PLAN_VALUE.format(count=count), False),
    ]
    first_due = min(schedule_rows.values(), default=(None, 0))[0]
    dates = [
        (ar.PDF_DELIVERY_DATE, _date(sale.purchase_date)),
        (ar.PRINT_FIRST_DUE, _date(first_due)),
        (ar.PDF_END_DATE, _date(sale.end_date)),
        (ar.PDF_CASH_PRICE, _money(sale.cash_price)),
    ]
    if include_sensitive:
        dates.extend([
            (ar.PDF_RATE, f"{sale.rate}%"),
            (ar.PDF_WHOLESALE_PRICE, _money(sale.wholesale_price)),
            (ar.PDF_PROFIT, _money(sale.profit)),
        ])

    compact = count > 6
    row_pad = "2px 6px" if compact else "3px 6px"
    schedule_html = []
    for index in sorted(schedule_rows):
        due_date, amount = schedule_rows[index]
        paid_date, paid_amount = paid.get(index, (None, 0))
        fill = SOFT if index % 2 == 0 else "#FFFFFF"
        settled = paid_amount >= amount > 0
        paid_cell = (
            f"<span style='color:{PAID if settled else DUE}; font-weight:bold'>{_escape(_money(paid_amount))}</span>"
            if paid_amount else "&nbsp;"
        )
        cell = f"style='background-color:{fill}; padding:{row_pad}"
        schedule_html.append(_tr(
            f"<td {cell}; color:{NAVY}; font-weight:bold'>{index:02d}</td>",
            f"<td {cell}'>{_escape(_date(due_date))}</td>",
            f"<td {cell}; font-weight:bold'>{_escape(_money(amount))}</td>",
            f"<td {cell}'>{_escape(_date(paid_date)) if paid_date else '&nbsp;'}</td>",
            f"<td {cell}'>{paid_cell}</td>",
            f"<td {cell}'>&nbsp;</td>",
        ))
    schedule_head = _tr(*(
        f"<th width='{width}%'>{_escape(title)}</th>"
        for width, title in (
            (8, ar.PDF_COL_NUMBER), (18, ar.PRINT_COL_DUE_DATE), (18, ar.PDF_COL_MONTHLY_PAYMENT),
            (18, ar.PRINT_COL_PAID_ON), (18, ar.PRINT_COL_PAID_AMOUNT), (20, ar.PRINT_COL_SIGNATURE),
        )
    ))

    clauses = "".join(
        _tr(f"<td width='22' class='clause-no'>{number}</td>",
            f"<td class='clause'>{_fill_html(clause, values)}</td>")
        for number, clause in enumerate(templates.commitment_clauses, start=1)
    )
    clauses_html = (
        f"<table width='100%' cellspacing='0' cellpadding='0'>{clauses}</table>" if clauses else ""
    )
    signatures = templates.commitment_signatures
    signature_row = _tr(*(
        f"<td width='{100 // len(signatures)}%' class='signature'>{_escape(label)}"
        f"<br><br><br><span style='color:{LINE}'>{'.' * 34}</span></td>"
        for label in signatures
    ))
    header_row = _tr(
        f"<td width='40%' style='vertical-align: middle;'>{_shop_block(templates)}</td>",
        "<td width='36%' align='center' style='vertical-align: middle;'>"
        f"<div class='title' align='center'>{_escape(templates.commitment_title)}</div>"
        f"<div class='meta' align='center'>{_escape(ar.PRINT_CONTRACT_NUMBER)} <b>{_ltr(f'{sale.id:06d}' if sale.id else '—')}</b>"
        f" &nbsp;·&nbsp; {_escape(_date(sale.purchase_date))}</div></td>",
        f"<td width='24%' align='{_end_side()}' style='vertical-align: middle;'>"
        f"<img src='{_escape(logo_uri)}' width='84' height='84'></td>",
    )
    product_row = _tr(
        f"<td class='field'><div class='label'>{_escape(ar.PDF_PRODUCT)}</div>"
        f"<div class='value'><span style='font-size: 13pt'>{_RLM if is_rtl() else ''}"
        f"{_ltr_html(_product_name(sale))}</span>{_product_details(sale)}</div></td>",
    )
    figures_row = _tr(*(_figure(caption, value, hot) for caption, value, hot in figures))
    facts_row = _tr(*(
        f"<td class='fact'>{_escape(label)}: <b>{_escape(value)}</b></td>" for label, value in dates
    ))
    footer = _fill_html(templates.commitment_footer, values) if templates.commitment_footer else ""

    return f"""<!doctype html>
<html dir="{_direction()}"><head><meta charset="utf-8"><style>
body {{ direction: {_direction()}; font-family: '{_cairo_font_family()}'; color: {INK}; font-size: 1pt; }}
/* Text gets its own size; the 1pt body only shrinks the empty line Qt adds after each table. */
td, th, div, p {{ font-size: 10pt; line-height: 88%; }}
td {{ vertical-align: middle; }}
.title {{ font-size: 23pt; font-weight: bold; color: {NAVY}; }}
.meta {{ font-size: 8.5pt; color: {MUTED}; }}
.ids {{ font-size: 8pt; color: {MUTED}; }}
.section {{ font-size: 10.5pt; font-weight: bold; color: {NAVY}; background-color: {SOFT}; padding: 3px 9px; }}
.field {{ padding: 3px 2px 1px 2px; border-bottom: 1px dotted #94A3B8; vertical-align: bottom; }}
.label {{ font-size: 7.5pt; color: {MUTED}; font-weight: bold; }}
.value {{ font-size: 10.5pt; font-weight: bold; color: {INK}; }}
.figure {{ background-color: {SOFT}; padding: 4px 4px; border: 1px solid {LINE}; text-align: center; }}
.figure-hot {{ background-color: {NAVY}; padding: 4px 4px; border: 1px solid {NAVY}; text-align: center; color: #FFFFFF; }}
.caption {{ font-size: 7.5pt; color: {MUTED}; }}
.caption-hot {{ font-size: 7.5pt; color: #C7D7F5; }}
.number {{ font-size: 12.5pt; font-weight: bold; }}
.fact {{ font-size: 8.5pt; color: {MUTED}; padding: 3px 4px; }}
.fact b {{ color: {INK}; }}
.intro {{ font-size: 10.5pt; line-height: 125%; padding: 2px 4px; }}
.clause-no {{ font-weight: bold; color: {BLUE}; font-size: 10pt; padding: 2px 4px; vertical-align: top; }}
.clause {{ font-size: 9.5pt; line-height: 112%; padding: 1px 4px; }}
.schedule th {{ background-color: {NAVY}; color: #FFFFFF; font-size: 8.5pt; padding: 5px 4px; }}
.schedule td {{ font-size: 9.5pt; text-align: center; border-bottom: 1px solid #E2E8F0; }}
.note {{ font-size: 8.5pt; color: {MUTED}; padding: 2px 2px; }}
.signature {{ border: 1px solid {LINE}; padding: 6px 4px 4px 4px; text-align: center; font-size: 8.5pt;
              font-weight: bold; color: {INK}; vertical-align: top; }}
.footer {{ font-size: 8.5pt; color: {MUTED}; }}
</style></head><body>
<table width="100%" cellspacing="0" cellpadding="0">{header_row}</table>
{_rule()}
{_section(ar.PRINT_SECTION_CUSTOMER)}
<table width="100%" cellspacing="4" cellpadding="0">{''.join(_field_row(row) for row in customer_rows)}</table>
{_gap(6)}
{_section(ar.PRINT_SECTION_PRODUCT)}
<table width="100%" cellspacing="6" cellpadding="0">{product_row}</table>
{_gap(6)}
<table width="100%" cellspacing="5" cellpadding="0">{figures_row}</table>
<table width="100%" cellspacing="0" cellpadding="0">{facts_row}</table>
{_gap(6)}
{_section(ar.PRINT_SECTION_COMMITMENT)}
<div class="intro">{_fill_html(templates.commitment_intro, values)}</div>
{clauses_html}
{_gap(6)}
{_section(ar.PDF_SCHEDULE_TITLE)}
<table class="schedule" width="100%" cellspacing="0" cellpadding="0">
{schedule_head}
{''.join(schedule_html)}
</table>
<div class="note">{_escape(_commitment_due_note(sale, schedule_rows))}</div>
{_gap(4)}
<table width="100%" cellspacing="6" cellpadding="0">{signature_row}</table>
{f'<div class="footer" align="center">{footer}</div>' if footer else ''}
{_ids_footer(templates, business_nis, business_nif)}
{_contact_line(templates)}
</body></html>"""


def _commitment_schedule(sale: Sale) -> dict[int, tuple[date, int]]:
    """Return installment due date and amount keyed by one-based row number."""
    fallback = {
        draft.installment_index: (draft.due_date, draft.amount_due)
        for draft in schedule.build(sale, {"due_mode": schedule.DEFAULT_DUE_MODE})
    }
    stored: dict[int, tuple[date, int]] = {}
    for installment in sale.installments:
        stored[installment.installment_index] = (installment.due_date, installment.amount_due)
    fallback.update(stored)
    return fallback


def _paid_by_installment(sale: Sale) -> dict[int, tuple[date | None, int]]:
    """Date and amount already received for each installment (ledger max rule)."""
    result: dict[int, tuple[date | None, int]] = {}
    for installment in sale.installments:
        recorded = list(installment.payments or [])
        amount = max(installment.amount_paid or 0, sum(payment.amount for payment in recorded))
        if amount <= 0:
            continue
        last = max((payment.payment_date for payment in recorded), default=None)
        result[installment.installment_index] = (installment.paid_date or last, amount)
    return result


def _commitment_due_note(sale: Sale, schedule_rows: dict[int, tuple[date, int]]) -> str:
    """Describe the actual saved due-date pattern on the printed contract."""
    due_days = {due_date.day for due_date, _amount in schedule_rows.values()}
    if due_days == {1}:
        return ar.PDF_NOTE_FIRST_OF_MONTH
    if due_days == {sale.purchase_date.day}:
        return ar.PDF_NOTE_PURCHASE_DAY.format(day=sale.purchase_date.day)
    return ar.PDF_NOTE_SCHEDULE


def _placeholder_values(
    customer: Customer,
    sale: Sale,
    schedule_rows: Mapping[int, tuple[date, int]],
    templates: PrintTemplates,
) -> dict[str, str]:
    """Values of the ``{placeholders}`` usable in the commitment texts."""
    first_due = min(schedule_rows.values(), default=(None, 0))[0]
    return {
        "customer": customer.full_name or "",
        "phone": _ltr(customer.phone or ""),
        "product": _ltr(_product_name(sale)),
        "total": _money(sale.total),
        "down_payment": _money(sale.down_payment),
        "financed": _money(sale.financed),
        "monthly": _money(sale.monthly_amount),
        "months": str(sale.months or ""),
        "count": str(len(schedule_rows)),
        "first_due": _date(first_due),
        "end_date": _date(sale.end_date),
        "purchase_date": _date(sale.purchase_date),
        "day": str(sale.purchase_date.day),
        "shop": _ltr(templates.shop_name),
        "location": _ltr(templates.shop_location),
        "today": _date(date.today()),
    }


# ------------------------------------------------------------------ receipts
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
    business_nis: str | None = None,
    business_nif: str | None = None,
    *,
    items: Sequence[ReceiptItem] | None = None,
    receipt_number: int | None = None,
    templates: PrintTemplates | None = None,
) -> str:
    """The logo, the business IDs, then the sale's nine facts (owner's choice: no shop line or signer).

    ``expected_pay_date`` and ``issued_by`` are accepted for callers but are
    not printed on this receipt. With ``items`` the product row lists each
    product with its quantity and amount.
    """
    templates = templates or default_templates()
    settled = remaining_balance <= 0
    ids_html = _business_ids(templates, business_nis, business_nif)

    mark = _RLM if is_rtl() else ""
    if items:
        product_html = "<br>".join(
            f"{mark}{_ltr_html(f'{item.name} ×{item.quantity}' if item.quantity > 1 else item.name)}"
            f" {mark}<span style='color:{MUTED}; font-weight:normal'>— {_escape(_money(item.amount))}</span>"
            for item in items
        )
    else:
        product_html = mark + _ltr_html(product)
    kind = ar.PDF_CASH_KIND_CASH if sale_type == "cash" else ar.PDF_CASH_KIND_CREDIT
    status_text = ar.PDF_CASH_STATUS_PAID if settled else ar.PDF_CASH_STATUS_DUE
    status_color, status_fill = (PAID, PAID_FILL) if settled else (DUE, DUE_FILL)
    facts: list[tuple[str, str, str]] = [
        # (label, value html, row style)
        (ar.PDF_CASH_DATE, _escape(_date(purchase_date)), ""),
        (ar.PDF_CUSTOMER_NAME, _escape(customer_name), ""),
        (ar.PDF_PHONE, _ltr_html(customer_phone) or _escape(ar.PDF_VALUE_MISSING), ""),
        (ar.PDF_PRODUCT, product_html, ""),
        (ar.PDF_CASH_KIND, _escape(kind), ""),
        (ar.CASH_TOTAL_INVOICE, _escape(_money(total)), "total"),
        (ar.CASH_AMOUNT_PAID, _escape(_money(amount_paid)), ""),
        (ar.CASH_REMAINING_BALANCE, _escape(_money(remaining_balance)), "" if settled else "due"),
        (ar.PDF_CASH_STATUS, f"{'✓ ' if settled else ''}{_escape(status_text)}", "status"),
    ]
    rows = []
    for index, (label, value, style) in enumerate(facts):
        if style == "total":
            label_css = f"background-color:{NAVY}; color:#C7D7F5; font-size:10pt; font-weight:bold;"
            value_css = f"background-color:{NAVY}; color:#FFFFFF; font-size:14pt; font-weight:bold;"
        elif style == "status":
            label_css = f"background-color:{status_fill}; color:{MUTED};"
            value_css = f"background-color:{status_fill}; color:{status_color}; font-size:11pt; font-weight:bold;"
        else:
            fill = SOFT if index % 2 == 0 else "#FFFFFF"
            color = DUE if style == "due" else INK
            label_css = f"background-color:{fill}; color:{MUTED};"
            value_css = f"background-color:{fill}; color:{color}; font-weight:bold;"
        rows.append(_tr(
            f"<td width='42%' style='{label_css} padding:7px 10px;'>{_escape(label)}</td>",
            f"<td style='{value_css} padding:7px 10px;'>{value}</td>",
            css_class="fact",
        ))
    number_html = (
        f"<div class='meta' align='center'>{_escape(ar.PRINT_RECEIPT_NUMBER)} <b>{_ltr(f'{receipt_number:06d}')}</b></div>"
        if receipt_number else ""
    )
    return f"""<!doctype html>
<html dir="{_direction()}"><head><meta charset="utf-8"><style>
body {{ direction: {_direction()}; font-family: '{_cairo_font_family()}'; color: {INK}; font-size: 1pt; }}
/* Text gets its own size; the 1pt body only shrinks the empty line Qt adds after each table. */
td, th, div, p {{ font-size: 10pt; }}
.title {{ font-size: 17pt; font-weight: bold; color: {NAVY}; }}
.meta {{ font-size: 8.5pt; color: {MUTED}; }}
.ids {{ font-size: 8pt; color: {MUTED}; }}
.thanks {{ font-size: 9pt; color: {MUTED}; }}
</style></head><body>
<p align="center" style="margin: 0;"><img src="{_escape(logo_uri)}" width="96" height="96"></p>
<div class="title" align="center">{_escape(ar.CASH_RECEIPT_TITLE)}</div>
{number_html}
{f'<div class="ids" align="center">{ids_html}</div>' if ids_html else ''}
{_gap(8)}
<table width="100%" cellspacing="0" cellpadding="0" style="border: 1px solid {LINE};">
{''.join(rows)}
</table>
{_gap(10)}
{''.join(f'<div class="thanks" align="center">{_escape(line)}</div>' for line in templates.receipt_footer_lines)}
</body></html>"""


def _receipt_payment_details(sale: Sale, payment: Payment) -> dict[str, object]:
    """Verify payment membership; the installment, its balance after the payment, and the sale's."""
    if sale.sale_type == "credit":
        if payment.sale_id != sale.id:
            raise ValueError("Payment does not belong to this credit sale")
        history = sorted(sale.credit_payments, key=lambda item: item.id)
        if not any(item.id == payment.id for item in history):
            raise ValueError("Payment is not present in the loaded credit history")
        paid_through = sum(item.amount for item in history if item.id <= payment.id)
        balance = max(0, sale.financed - paid_through)
        return {"installment": None, "balance_after": balance, "sale_balance_after": balance}

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
    sale_paid = 0
    for row in sale.installments:
        rows_payments = list(row.payments or [])
        sale_paid += max(0, row.amount_paid - sum(item.amount for item in rows_payments))
        sale_paid += sum(item.amount for item in rows_payments if item.id <= payment.id)
    return {
        "installment": installment,
        "balance_after": max(0, installment.amount_due - legacy_paid - paid_through),
        "sale_balance_after": max(0, sale.financed - sale_paid),
    }


def _batch_receipt_details(sales: tuple[Sale, ...], sale: Sale, payment: Payment) -> dict[str, object]:
    """The total, installments and balance of the payment's whole batch ({} for a payment on its own)."""
    if sale not in sales:
        sales = (sale, *sales)
    parts = [
        item for one in sales for item in _sale_payments(one)
        if payment.batch_id and item.batch_id == payment.batch_id
    ] or [payment]
    if len(parts) == 1 and len(sales) == 1:
        return {}
    part_ids = {item.id for item in parts}
    paid_sales = [one for one in sales if any(item.id in part_ids for item in _sale_payments(one))] or [sale]
    last = max(part_ids)
    numbers = sorted({
        row.installment_index for one in paid_sales for row in one.installments
        if any(item.id in part_ids for item in row.payments or [])
    })
    details: dict[str, object] = {
        "amount": sum(item.amount for item in parts),
        "sale_balance_after": max(0, sum(one.financed - _paid_through(one, last) for one in paid_sales)),
        "products": tuple(dict.fromkeys(_product_name(one) for one in paid_sales)),
    }
    if len(numbers) > 1:
        details["installment"] = None
        details["installment_numbers"] = tuple(numbers)
    return details


def _sale_payments(sale: Sale) -> list[Payment]:
    """Every payment row of a loaded sale (credit payments or installment payments)."""
    if sale.sale_type == "credit":
        return list(sale.credit_payments or [])
    return [item for row in sale.installments for item in row.payments or []]


def _paid_through(sale: Sale, last_payment_id: int) -> int:
    """What was paid on the sale's financed part up to and including payment ``last_payment_id``."""
    if sale.sale_type == "credit":
        return sum(item.amount for item in sale.credit_payments or [] if item.id <= last_payment_id)
    paid = 0
    for row in sale.installments:
        recorded = list(row.payments or [])
        paid += max(0, row.amount_paid - sum(item.amount for item in recorded))
        paid += sum(item.amount for item in recorded if item.id <= last_payment_id)
    return paid


def _receipt_html(
    customer: Customer,
    sale: Sale,
    payment: Payment,
    payment_details: dict[str, object],
    logo_uri: str,
    issued_by: str | None,
    templates: PrintTemplates | None = None,
    business_nis: str | None = None,
    business_nif: str | None = None,
) -> str:
    """A5 receipt of one installment or credit payment: the amount first, then the details."""
    templates = templates or default_templates()
    installment = payment_details["installment"]
    sale_balance = int(payment_details.get("sale_balance_after", payment_details["balance_after"]))
    amount = int(payment_details.get("amount", payment.amount))
    products = payment_details.get("products") or (_product_name(sale),)
    fields: list[tuple[str, str]] = [
        (ar.PDF_CUSTOMER_NAME, _escape(customer.full_name)),
        (ar.PDF_PHONE, _ltr_html(customer.phone) or _escape(ar.PDF_VALUE_MISSING)),
        (ar.PDF_PRODUCT, " + ".join(_ltr_html(name) for name in products)),
        (ar.PDF_SALE_TYPE, _escape(_sale_type(sale.sale_type))),
        (ar.PDF_PAYMENT_DATE, _escape(_date(payment.payment_date))),
        (ar.PDF_PAYMENT_METHOD, _escape(_payment_method(payment.method))),
    ]
    if isinstance(installment, Installment):
        fields.extend((
            (ar.PDF_INSTALLMENT_NUMBER, _escape(str(installment.installment_index))),
            (ar.PDF_INSTALLMENT_DUE, _escape(_date(installment.due_date))),
            (ar.PDF_INSTALLMENT_AMOUNT, _escape(_money(installment.amount_due))),
            (ar.PRINT_INSTALLMENT_LEFT, _escape(_money(int(payment_details["balance_after"])))),
        ))
    elif payment_details.get("installment_numbers"):
        numbers = ("، " if is_rtl() else ", ").join(str(n) for n in payment_details["installment_numbers"])
        fields.append((ar.PRINT_INSTALLMENTS_PAID, _escape(numbers)))
    if payment.note:
        fields.append((ar.PDF_PAYMENT_NOTE, _escape(payment.note)))
    if issued_by:
        fields.append((ar.PDF_RECEIVED_BY, _ltr_html(issued_by)))
    rows = "".join(
        _tr(
            f"<td width='42%' style='background-color:{SOFT if index % 2 == 0 else '#FFFFFF'}; color:{MUTED};"
            f" padding:6px 10px;'>{_escape(label)}</td>",
            f"<td style='background-color:{SOFT if index % 2 == 0 else '#FFFFFF'}; font-weight:bold;"
            f" padding:6px 10px;'>{value}</td>",
        )
        for index, (label, value) in enumerate(fields)
    )
    settled = sale_balance <= 0
    balance_color, balance_fill = (PAID, PAID_FILL) if settled else (DUE, DUE_FILL)
    end = _end_side()
    header_row = _tr(
        f"<td width='62%' style='vertical-align: middle;'>{_shop_block(templates, compact=True)}"
        f"{_ids_line(templates, business_nis, business_nif)}</td>",
        f"<td width='38%' align='{end}' style='vertical-align: middle;'>"
        f"<img src='{_escape(logo_uri)}' width='70' height='70'></td>",
    )
    title_row = _tr(
        f"<td style='vertical-align: middle;'><span class='title'>{_escape(ar.PDF_RECEIPT_TITLE)}</span></td>",
        f"<td align='{end}' class='meta' style='vertical-align: middle;'>{_escape(ar.PDF_RECEIPT_NUMBER)}"
        f" <b>{_ltr(f'{payment.id:06d}')}</b><br>{_escape(ar.PDF_RECEIPT_DATE)} <b>{_escape(_date(date.today()))}</b></td>",
    )
    amount_row = _tr(
        f"<td style='background-color:{NAVY}; color:#C7D7F5; padding:8px 12px; font-size:10pt; font-weight:bold;'>"
        f"{_escape(ar.PDF_PAID_AMOUNT)}</td>",
        f"<td align='{end}' style='background-color:{NAVY}; color:#FFFFFF; padding:8px 12px; font-size:17pt;"
        f" font-weight:bold;'>{_escape(_money(amount))}</td>",
    )
    balance_row = _tr(
        f"<td style='background-color:{balance_fill}; color:{MUTED}; padding:7px 12px;'>"
        f"{_escape(ar.PDF_REMAINING_BALANCE)}</td>",
        f"<td align='{end}' style='background-color:{balance_fill}; color:{balance_color}; padding:7px 12px;"
        f" font-size:12pt; font-weight:bold;'>{'✓ ' if settled else ''}{_escape(_money(sale_balance))}</td>",
    )
    signature_row = _tr(
        "<td width='50%'>&nbsp;</td>",
        f"<td width='50%' align='center' class='meta'>{_escape(ar.PDF_RECEIVER_SIGNATURE)}<br><br><br>"
        f"<span style='color:{LINE}'>{'.' * 34}</span></td>",
    )
    return f"""<!doctype html>
<html dir="{_direction()}"><head><meta charset="utf-8"><style>
body {{ direction: {_direction()}; font-family: '{_cairo_font_family()}'; color: {INK}; font-size: 1pt; }}
/* Text gets its own size; the 1pt body only shrinks the empty line Qt adds after each table. */
td, th, div, p {{ font-size: 10pt; }}
.title {{ font-size: 16pt; font-weight: bold; color: {NAVY}; }}
.meta {{ font-size: 8.5pt; color: {MUTED}; }}
.ids {{ font-size: 8pt; color: {MUTED}; }}
.thanks {{ font-size: 9pt; color: {MUTED}; }}
</style></head><body>
<table width="100%" cellspacing="0" cellpadding="0">{header_row}</table>
{_rule()}
<table width="100%" cellspacing="0" cellpadding="0">{title_row}</table>
{_gap(6)}
<table width="100%" cellspacing="0" cellpadding="0">{amount_row}</table>
{_gap(6)}
<table width="100%" cellspacing="0" cellpadding="0" style="border: 1px solid {LINE};">{rows}</table>
{_gap(6)}
<table width="100%" cellspacing="0" cellpadding="0">{balance_row}</table>
{_gap(14)}
<table width="100%" cellspacing="0" cellpadding="0">{signature_row}</table>
{_gap(8)}
{''.join(f'<div class="thanks" align="center">{_escape(line)}</div>' for line in templates.receipt_footer_lines)}
{_contact_line(templates)}
</body></html>"""


# ------------------------------------------------------------ HTML helpers
def _shop_block(templates: PrintTemplates, *, compact: bool = False) -> str:
    """The shop's name, place and header lines (the boxed corner of the paper form)."""
    location = f" - {templates.shop_location}" if templates.shop_location else ""
    size = "16pt" if compact else "21pt"
    lines = "".join(
        f"<div style='font-size:{'8pt' if compact else '9pt'}; color:{INK}'>{_escape(line)}</div>"
        for line in templates.header_lines
    )
    # One left-to-right run for "AkremMobile - Oum Thiour" (Latin text in an Arabic header).
    latin = not _has_rtl(templates.shop_name + location)
    start, stop = ("\u202a", "\u202c") if latin else ("", "")
    return (
        f"<div style=\"font-family:'{_rajdhani_font_family()}'; font-size:{size}; font-weight:bold; color:{NAVY}\">"
        f"{start}{_escape(templates.shop_name)}<span style='color:{BLUE}'>{_escape(location)}</span>{stop}</div>{lines}"
    )


def _business_ids(
    templates: PrintTemplates, business_nis: str | None = None, business_nif: str | None = None
) -> str:
    """The shop's NIS, NIF and RC, one small left-to-right run, empty when none is set.

    The saved print templates are the source; the arguments only override them
    (the sale receipt passes the values a caller already had).
    """
    values = [
        f"{label}: {value}" for label, value in _business_id_values(templates, business_nis, business_nif)
    ]
    return " &nbsp;·&nbsp; ".join(_ltr_html(value) for value in values if value.split(": ", 1)[1])


def _business_id_values(
    templates: PrintTemplates, business_nis: str | None = None, business_nif: str | None = None
) -> list[tuple[str, str]]:
    """``(label, value)`` of the shop's NIS, NIF and RC (a value may be blank)."""
    return [
        ("NIS", (business_nis or "").strip() or templates.business_nis),
        ("NIF", (business_nif or "").strip() or templates.business_nif),
        ("RC", templates.business_rc),
    ]


def _ids_footer(
    templates: PrintTemplates, business_nis: str | None = None, business_nif: str | None = None
) -> str:
    """The commitment's last lines, like an official form: a rule, then RC, NIS and NIF.

    The three always print; a number the owner has not entered is a dotted
    line to fill in by hand.
    """
    values = dict(_business_id_values(templates, business_nis, business_nif))
    cells = "".join(
        f"<td width='33%' align='center' style='font-size:8.5pt; color:{INK}; padding:3px 2px'>"
        f"{_ltr_html(f'{label} : ' + values[label]) if values[label] else _blank_id(label)}</td>"
        for label in ("RC", "NIS", "NIF")
    )
    return (
        "<table width='100%' cellspacing='0' cellpadding='0' style='margin-top:6px'>"
        f"<tr><td style='background-color:{LINE}; font-size:0.5pt'>&nbsp;</td></tr></table>"
        f"<table width='100%' cellspacing='0' cellpadding='0'><tr>{cells}</tr></table>"
    )


def _blank_id(label: str) -> str:
    """``RC : ......`` in reading order: a number the owner has not entered, to fill in by hand."""
    return f"‪{_escape(label)} : <span style='color:{LINE}'>{'.' * 22}</span>‬"


def _ids_line(
    templates: PrintTemplates, business_nis: str | None = None, business_nif: str | None = None
) -> str:
    """The business IDs under the shop's name (the ``""`` of an empty document header stays)."""
    ids = _business_ids(templates, business_nis, business_nif)
    return f"<div class='ids'>{ids}</div>" if ids else ""


def _contact_line(templates: PrintTemplates) -> str:
    """Phone and address of the shop, when the owner filled them in."""
    parts = [_ltr_html(templates.shop_phone), _escape(templates.shop_address)]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    return f"<div align='center' style='font-size:8pt; color:{MUTED}'>{' &nbsp;·&nbsp; '.join(parts)}</div>"


def _rule() -> str:
    """The navy and blue double line under a document header."""
    return (
        "<table width='100%' cellspacing='0' cellpadding='0' style='margin-top:4px; margin-bottom:8px'>"
        f"<tr><td style='background-color:{NAVY}; font-size:1pt'>&nbsp;</td></tr>"
        "<tr><td style='font-size:1pt'>&nbsp;</td></tr>"
        f"<tr><td style='background-color:{BLUE}; font-size:0.5pt'>&nbsp;</td></tr></table>"
    )


def _section(title: str) -> str:
    """A section heading with a blue accent bar on the reading-start side."""
    row = _tr(
        f"<td width='5' style='background-color:{BLUE}'>&nbsp;</td>",
        f"<td class='section'>{_escape(title)}</td>",
    )
    return f"<table width='100%' cellspacing='0' cellpadding='0' style='margin-bottom:2px'>{row}</table>"


def _tr(*cells: str, css_class: str = "") -> str:
    """A table row with its cells in reading order.

    Qt lays table cells out left to right even in a right-to-left document,
    so in Arabic the cells are written in reverse to put the first on the right.
    """
    ordered = reversed(cells) if is_rtl() else cells
    attribute = f" class='{css_class}'" if css_class else ""
    return f"<tr{attribute}>{''.join(ordered)}</tr>"


def _gap(pixels: int) -> str:
    """Vertical space between blocks."""
    return f"<div style='font-size:1pt; margin-top:{pixels}px'>&nbsp;</div>"


def _field_row(fields: Sequence[tuple[str, str, int]]) -> str:
    """One line of the customer grid: each field is a caption over its dotted value (blank to fill by hand)."""
    return _tr(*(
        f"<td width='{width}%' class='field'><div class='label'>{_escape(label)}</div>"
        f"<div class='value'>{value or '&nbsp;'}</div></td>"
        for label, value, width in fields
    ))


def _figure(caption: str, value: str, hot: bool) -> str:
    """A boxed key figure (the monthly payment stands out)."""
    box, caption_class = ("figure-hot", "caption-hot") if hot else ("figure", "caption")
    return (
        f"<td width='20%' class='{box}'><div class='{caption_class}' align='center'>{_escape(caption)}</div>"
        f"<div class='number' align='center'>{_escape(value)}</div></td>"
    )


def _product_details(sale: Sale) -> str:
    """Second line under the product: colour, battery, IMEI and REF when recorded."""
    from app.i18n.stock_text import battery_text

    parts: list[str] = []
    if sale.color:
        parts.append(f"{_escape(ar.PRINT_COLOR)}: {_ltr_html(sale.color)}")
    if sale.is_new or sale.battery_health is not None:
        parts.append(f"{_escape(ar.PRINT_BATTERY)}: {_escape(battery_text(sale.is_new, sale.battery_health))}")
    if sale.imei:
        parts.append(_ltr_html(f"IMEI {sale.imei}"))
    if sale.reference:
        reference = sale.reference if sale.reference.upper().startswith("REF") else f"REF {sale.reference}"
        parts.append(_ltr_html(reference))
    if not parts:
        return ""
    return f"<br><span style='font-size:8.5pt; color:{MUTED}; font-weight:normal'>{_join(parts)}</span>"


def _join(parts: Sequence[str]) -> str:
    """Join escaped parts with a dot; in Arabic each part keeps its place (Latin parts never merge)."""
    mark = _RLM if is_rtl() else ""
    return f" {mark}&nbsp;·&nbsp;{mark} ".join(parts)


def _product_name(sale: Sale) -> str:
    """Product name with the quantity when several units were sold."""
    quantity = getattr(sale, "quantity", 1) or 1
    return f"{sale.product} ×{quantity}" if quantity > 1 else sale.product


def _fill_html(text: str, values: Mapping[str, str]) -> str:
    """Escape a template text and put each known ``{placeholder}`` value in bold."""
    pieces: list[str] = []
    position = 0
    for match in re.finditer(r"\{([a-z_]+)\}", text):
        pieces.append(_escape(text[position:match.start()]))
        key = match.group(1)
        pieces.append(f"<b>{_escape(values[key])}</b>" if key in values else _escape(match.group(0)))
        position = match.end()
    pieces.append(_escape(text[position:]))
    return "".join(pieces)


def _ltr(text: str) -> str:
    """Keep Latin text (names, IMEI, REF, numbers) in reading order inside right-to-left text."""
    if not text or any(start <= ord(char) <= end for char in text for start, end in _RTL_RANGES):
        return text
    return f"\u202a{text}\u202c"  # LEFT-TO-RIGHT EMBEDDING ... POP DIRECTIONAL FORMATTING


def _has_rtl(text: str) -> bool:
    return any(start <= ord(char) <= end for char in text for start, end in _RTL_RANGES)


def _ltr_html(value: object) -> str:
    """Escaped, direction-safe text ("" for a missing value)."""
    if value is None or value == "":
        return ""
    return _escape(_ltr(str(value)))


def _text(value: object) -> str:
    """Escaped text ("" for a missing value)."""
    return "" if value is None or value == "" else _escape(value)


def _date_or_blank(value: date | None) -> str:
    return "" if value is None else _escape(_date(value))


def _direction() -> str:
    return "rtl" if is_rtl() else "ltr"


def _end_side() -> str:
    """HTML ``align`` value of the reading-end side (left in Arabic)."""
    return "left" if is_rtl() else "right"


# ------------------------------------------------------------------ rendering
def _commitment_page(templates: PrintTemplates) -> _PageSpec:
    return _PageSpec(QPageSize.PageSizeId.A4, margin_mm=13, frame_mm=7, watermark=templates.watermark)


def _receipt_page(templates: PrintTemplates) -> _PageSpec:
    return _PageSpec(QPageSize.PageSizeId.A5, margin_mm=11, frame_mm=6, watermark=templates.watermark)


class _Layout:
    """A document laid out for a page: shrunk to fit one page, or paginated."""

    def __init__(self, content: str, spec: _PageSpec) -> None:
        page = QPageSize(spec.size).size(QPageSize.Unit.Inch)
        self.page_size = QSizeF(page.width() * _DOC_DPI, page.height() * _DOC_DPI)
        self.margin = spec.margin_mm / 25.4 * _DOC_DPI
        self.frame = spec.frame_mm / 25.4 * _DOC_DPI
        self.spec = spec
        self.body = QSizeF(self.page_size.width() - 2 * self.margin, self.page_size.height() - 2 * self.margin)
        # Lay text out at 96 dpi whatever the screen, so the printed size never changes.
        self._device = QImage(1, 1, QImage.Format.Format_ARGB32)
        dots_per_meter = round(_DOC_DPI / 0.0254)
        self._device.setDotsPerMeterX(dots_per_meter)
        self._device.setDotsPerMeterY(dots_per_meter)
        self.document = QTextDocument()
        self.document.documentLayout().setPaintDevice(self._device)
        self.document.setDocumentMargin(0)
        # Tiny default: the empty line Qt adds after each table takes it (text sets its own size).
        self.document.setDefaultFont(QFont(_cairo_font_family(), 1))
        self.document.setHtml(content)
        self.scale = self._fit_scale()
        if self.scale is None:
            self.scale = 1.0
            self.document.setPageSize(self.body)
            self.pages = self.document.pageCount()
            self.paginated = True
        else:
            self.pages = 1
            self.paginated = False

    def _height_at(self, scale: float) -> float:
        self.document.setTextWidth(self.body.width() / scale)
        return self.document.size().height() * scale

    def _fit_scale(self) -> float | None:
        """The largest scale (down to ``_MIN_FIT_SCALE``) that keeps the document on one page."""
        if self._height_at(1.0) <= self.body.height():
            return 1.0
        if self._height_at(_MIN_FIT_SCALE) > self.body.height():
            return None
        low, high = _MIN_FIT_SCALE, 1.0
        for _ in range(9):
            middle = (low + high) / 2
            if self._height_at(middle) <= self.body.height():
                low = middle
            else:
                high = middle
        self._height_at(low)
        return low

    def paint_page(self, painter: QPainter, index: int, logo: QImage | None) -> None:
        """Paint page ``index`` in 1/96-inch units (the painter is already scaled)."""
        painter.save()
        _decorate(painter, self.page_size, self.frame, logo if self.spec.watermark else None)
        painter.translate(self.margin, self.margin)
        painter.scale(self.scale, self.scale)
        context = QAbstractTextDocumentLayout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, QColor(INK))
        if self.paginated:
            view = QRectF(0, index * self.body.height(), self.body.width(), self.body.height())
            painter.translate(0, -view.top())
            painter.setClipRect(view)
            context.clip = view
        self.document.documentLayout().draw(painter, context)
        painter.restore()


def _decorate(painter: QPainter, page: QSizeF, frame: float, logo: QImage | None) -> None:
    """The faint logo watermark and the thin double page frame."""
    if logo is not None and not logo.isNull():
        side = page.width() * 0.62
        target = QRectF((page.width() - side) / 2, (page.height() - side) / 2 + page.height() * 0.04, side, side)
        painter.save()
        painter.setOpacity(0.07)
        painter.drawImage(target, logo)
        painter.restore()
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(NAVY), 1.1))
    painter.drawRoundedRect(QRectF(frame, frame, page.width() - 2 * frame, page.height() - 2 * frame), 7, 7)
    inset = frame + 3.5
    painter.setPen(QPen(QColor(BLUE), 0.5))
    painter.drawRoundedRect(QRectF(inset, inset, page.width() - 2 * inset, page.height() - 2 * inset), 5, 5)
    painter.restore()


def _watermark_logo(logo_path: str | Path | None) -> QImage | None:
    """The logo's bright strokes in navy on a transparent background (its dark square dropped)."""
    logo = Path(logo_path) if logo_path is not None else RESOURCE_DIR / "logo.png"
    return _watermark_from(str(logo.resolve()))


@lru_cache(maxsize=4)
def _watermark_from(path: str) -> QImage | None:
    image = QImage(path)
    if image.isNull():
        return None
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    if gray.width() > 900:
        gray = gray.scaledToWidth(900, Qt.TransformationMode.SmoothTransformation)
    # Luminance becomes opacity: light strokes show, the dark background vanishes.
    alpha = QImage(gray.constBits(), gray.width(), gray.height(), gray.bytesPerLine(),
                   QImage.Format.Format_Alpha8).copy()
    tinted = QImage(gray.size(), QImage.Format.Format_ARGB32_Premultiplied)
    tinted.fill(QColor(NAVY))
    painter = QPainter(tinted)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    painter.drawImage(0, 0, alpha)
    painter.end()
    return tinted


def _write_pdf(
    path: Path,
    content: str,
    title: str,
    spec: _PageSpec,
    logo_path: str | Path | None = None,
) -> None:
    """Lay the HTML out and paint every page onto a 300 dpi PDF."""
    path.parent.mkdir(parents=True, exist_ok=True)
    layout = _Layout(content, spec)
    writer = QPdfWriter(str(path))
    writer.setTitle(title)
    writer.setCreator("AkremMobile")
    writer.setResolution(300)
    writer.setPageSize(QPageSize(spec.size))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
    logo = _watermark_logo(logo_path)
    painter = QPainter(writer)
    try:
        ratio = writer.resolution() / _DOC_DPI
        painter.scale(ratio, ratio)
        for index in range(layout.pages):
            if index:
                writer.newPage()
            layout.paint_page(painter, index, logo)
    finally:
        painter.end()
    del writer
    if not path.is_file() or path.stat().st_size == 0:
        raise OSError(f"Qt did not create the requested PDF: {path}")


def _render_image(content: str, spec: _PageSpec, width: int, logo_path: str | Path | None) -> QImage:
    """Page one of the document on a white image ``width`` pixels wide (the live preview)."""
    layout = _Layout(content, spec)
    ratio = width / layout.page_size.width()
    image = QImage(width, round(layout.page_size.height() * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("#FFFFFF"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.scale(ratio, ratio)
        layout.paint_page(painter, 0, _watermark_logo(logo_path))
    finally:
        painter.end()
    return image


def _logo_uri(path: str | Path | None) -> str:
    """Return a local URI of the logo cut to a circle (the bundled logo or a caller-provided one)."""
    logo = Path(path) if path is not None else RESOURCE_DIR / "logo.png"
    if not logo.is_file():
        raise FileNotFoundError(f"AkremMobile logo not found: {logo}")
    resolved = logo.resolve()
    return _round_logo(str(resolved), resolved.stat().st_mtime_ns).as_uri()


@lru_cache(maxsize=8)
def _round_logo(path: str, modified: int) -> Path:
    """The logo cropped to a circle on a transparent background, saved once per logo version.

    Every printed document shows the logo round instead of as a dark square.
    If the copy cannot be written, the original logo is used.
    """
    digest = hashlib.sha1(f"{path}|{modified}".encode("utf-8")).hexdigest()[:16]
    target = Path(tempfile.gettempdir()) / "AkremMobile" / f"logo_round_{digest}.png"
    if target.is_file():
        return target
    source = QImage(path)
    if source.isNull():
        return Path(path)
    side = min(source.width(), source.height())
    square = source.copy((source.width() - side) // 2, (source.height() - side) // 2, side, side)
    round_logo = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    round_logo.fill(Qt.GlobalColor.transparent)
    painter = QPainter(round_logo)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        clip = QPainterPath()
        clip.addEllipse(QRectF(0.5, 0.5, side - 1, side - 1))
        painter.setClipPath(clip)
        painter.drawImage(0, 0, square)
    finally:
        painter.end()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".tmp.png")
        if not round_logo.save(str(partial), "PNG"):
            return Path(path)
        partial.replace(target)
    except OSError:
        return Path(path)
    return target


@lru_cache(maxsize=1)
def _cairo_font_family() -> str:
    """Register and return the bundled Cairo family for portable PDF output."""
    return _register_font("Cairo-Variable.ttf", "Cairo")


@lru_cache(maxsize=1)
def _rajdhani_font_family() -> str:
    """Register and return the bundled Rajdhani family (the wordmark)."""
    return _register_font("Rajdhani-Bold.ttf", "Rajdhani")


def _register_font(filename: str, fallback: str) -> str:
    font_id = QFontDatabase.addApplicationFont(str(RESOURCE_DIR / "fonts" / filename))
    if font_id >= 0:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            return families[0]
    return fallback


def _pdf_path(path: str | Path) -> Path:
    """Resolve an output path and append ``.pdf`` when it has no PDF suffix."""
    output = Path(path).expanduser()
    if output.suffix.lower() != ".pdf":
        output = output.with_suffix(".pdf")
    return output.resolve()


# ------------------------------------------------------------- sample data
def _sample_documents() -> tuple[Customer, Sale, Payment]:
    """A made-up client, installment sale and payment for the live preview (never saved)."""
    customer = Customer(
        id=1, full_name=ar.PRINT_SAMPLE_CUSTOMER, phone="0661234567", id_number="109876543",
        id_issue_date=date(2020, 5, 3), id_issue_place=ar.PRINT_SAMPLE_PLACE, address=ar.PRINT_SAMPLE_ADDRESS,
        profession=ar.PRINT_SAMPLE_PROFESSION, ccp_number="0012345678 / 55", cheques_count=2, category_id=1,
    )
    purchase = date(2026, 7, 4)
    total, down, months = 60_750, 10_750, 6
    financed = total - down
    monthly = financed // months
    sale = Sale(
        id=7, customer_id=1, product="Honor X50 / X70", sale_type="installment", wholesale_price=38_000,
        cash_price=45_000, rate=35, down_payment=down, months=months, payment_interval=1, total=total,
        financed=financed, monthly_amount=monthly, profit=total - 38_000, purchase_date=purchase,
        end_date=calc.add_months(purchase, months), color="Black", is_new=True, imei="356789123456789",
        reference="REF-00042", quantity=1,
    )
    amounts = [monthly] * (months - 1) + [financed - monthly * (months - 1)]
    sale.installments = [
        Installment(id=index, sale_id=7, installment_index=index, due_date=calc.add_months(purchase, index),
                    amount_due=amounts[index - 1], amount_paid=0)
        for index in range(1, months + 1)
    ]
    payment = Payment(id=31, installment_id=1, amount=monthly, payment_date=date(2026, 8, 4), method="cash")
    first = sale.installments[0]
    first.amount_paid = monthly
    first.payments = [payment]
    for installment in sale.installments[1:]:
        installment.payments = []
    sale.credit_payments = []
    return customer, sale, payment


# ------------------------------------------------------------ small formats
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


def _escape(value: object) -> str:
    """HTML-escape all dynamic form data and localized labels."""
    return html.escape(str(value), quote=True)


__all__ = [
    "ReceiptItem",
    "commitment_html",
    "generate_cash_sale_receipt_pdf",
    "generate_commitment_pdf",
    "generate_payment_receipt_pdf",
    "preview_image",
    "write_preview_pdf",
]
