"""Owner-editable printed texts (commitment form, receipts) and the redesigned PDFs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from app.db.models import Installment
from app.db.session import session_scope
from app.i18n import ar
from app.services import auth, calc, pdf_forms, print_templates, settings
from app.services.print_templates import PrintTemplates

PASSWORD = "StrongPass123"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def session(memory_engine: Engine) -> Iterator[Session]:
    with session_scope(memory_engine) as scoped:
        yield scoped


@pytest.fixture
def owner_id(session: Session) -> int:
    return auth.create_first_owner(
        session, username="owner", password=PASSWORD, password_confirmation=PASSWORD
    ).id


# ------------------------------------------------------------------ service
def test_defaults_follow_the_paper_form(session: Session) -> None:
    templates = print_templates.load(session)

    assert templates == print_templates.default_templates()
    assert templates.shop_name == "AkremMobile"
    assert templates.commitment_title == ar.PDF_COMMITMENT_TITLE
    assert len(templates.commitment_signatures) == 3
    assert templates.commitment_clauses and templates.receipt_footer_lines
    assert templates.commitment_owner_figures is False and templates.watermark is True


def test_owner_saves_lines_and_blank_lines_are_dropped(session: Session, owner_id: int) -> None:
    saved = print_templates.save(session, owner_id, {
        "commitment_title": "  عقد بيع بالتقسيط ",
        "commitment_clauses": ["بند أول", "   ", "بند ثان"],
        "header_lines": [],
        "commitment_signatures": ["الزبون", "البائع", "الشاهد", "البلدية", "زائد"],
        "watermark": False,
    })

    assert saved.commitment_title == "عقد بيع بالتقسيط"
    assert saved.commitment_clauses == ("بند أول", "بند ثان")
    assert saved.header_lines == ()  # an emptied list stays empty
    assert len(saved.commitment_signatures) == print_templates.MAX_SIGNATURES
    assert saved.watermark is False
    assert print_templates.load(session) == saved


def test_bad_values_fall_back_to_the_defaults(session: Session) -> None:
    defaults = print_templates.default_templates()
    templates = print_templates.normalize({
        "shop_name": "   ",
        "commitment_signatures": [],
        "commitment_clauses": "not a list",
        "watermark": "yes",
        "commitment_intro": 42,
        "receipt_footer_lines": ["x" * 2000],
    })

    assert templates.shop_name == defaults.shop_name
    assert templates.commitment_signatures == defaults.commitment_signatures
    assert templates.commitment_clauses == defaults.commitment_clauses
    assert templates.watermark is True
    assert templates.commitment_intro == defaults.commitment_intro
    assert len(templates.receipt_footer_lines[0]) == print_templates.MAX_TEXT_LENGTH


def test_only_the_owner_can_change_or_reset_the_texts(session: Session, owner_id: int) -> None:
    seller = auth.create_seller(
        session, owner_id, username="seller", password=PASSWORD, password_confirmation=PASSWORD
    )
    with pytest.raises(auth.AuthorizationError):
        print_templates.save(session, seller.id, {"commitment_title": "X"})
    print_templates.save(session, owner_id, {"commitment_title": "X"})
    with pytest.raises(auth.AuthorizationError):
        print_templates.reset(session, seller.id)

    assert print_templates.reset(session, owner_id).commitment_title == ar.PDF_COMMITMENT_TITLE


def test_older_nis_and_nif_settings_are_reused(session: Session, owner_id: int) -> None:
    settings.set_value(session, "business_nis", "123456789")
    settings.set_value(session, "business_nif", "000198765432")

    assert print_templates.load(session).business_nis == "123456789"
    print_templates.save(session, owner_id, {"business_nis": "999"})
    loaded = print_templates.load(session)
    # A NIF left blank on the print page still prints the one typed in Settings.
    assert (loaded.business_nis, loaded.business_nif) == ("999", "000198765432")


def test_placeholders_are_filled_and_unknown_ones_kept() -> None:
    text = "أنا {customer} أدفع {monthly} {unknown} {"
    assert print_templates.fill(text, {"customer": "علي", "monthly": "5,000 دج"}) == (
        "أنا علي أدفع 5,000 دج {unknown} {"
    )


# ------------------------------------------------------------------ documents
def _twelve_payments():
    customer, sale, _payment = pdf_forms._sample_documents()
    sale.months = 12
    sale.monthly_amount = sale.financed // 12
    amounts = [sale.monthly_amount] * 11 + [sale.financed - sale.monthly_amount * 11]
    sale.installments = [
        Installment(id=k, sale_id=sale.id, installment_index=k, due_date=calc.add_months(sale.purchase_date, k),
                    amount_due=amounts[k - 1], amount_paid=0)
        for k in range(1, 13)
    ]
    for installment in sale.installments:
        installment.payments = []
    return customer, sale


def test_the_commitment_form_prints_the_owner_texts(qapp) -> None:
    customer, sale, _payment = pdf_forms._sample_documents()
    templates = replace(
        print_templates.default_templates(),
        commitment_title="عقد بيع بالتقسيط",
        commitment_intro="أنا {customer} أدفع {monthly} كل شهر.",
        commitment_clauses=("بند خاص بالمحل",),
        commitment_signatures=("الزبون", "البائع"),
        commitment_footer="حرر في {today}",
    )

    html = pdf_forms.commitment_html(customer, sale, templates, "file:///logo.png")

    assert "عقد بيع بالتقسيط" in html and "بند خاص بالمحل" in html
    assert f"<b>{customer.full_name}</b>" in html  # placeholders become the client's data
    assert f"<b>{sale.monthly_amount:,} {ar.CURRENCY_SUFFIX}</b>" in html
    assert date.today().strftime("%d/%m/%Y") in html
    assert html.count("class='signature'") == 2
    for default_clause in print_templates.default_templates().commitment_clauses:
        assert default_clause not in html
    # The shop's profit and wholesale price stay off the client's copy unless asked for.
    assert ar.PDF_PROFIT not in html and ar.PDF_WHOLESALE_PRICE not in html
    owner_copy = pdf_forms.commitment_html(customer, sale, templates, "file:///logo.png", include_sensitive=True)
    assert ar.PDF_PROFIT in owner_copy


def test_latin_product_names_keep_their_reading_order(qapp) -> None:
    customer, sale, _payment = pdf_forms._sample_documents()
    html = pdf_forms.commitment_html(customer, sale, print_templates.default_templates(), "file:///logo.png")

    # "Honor X50 / X70" is one left-to-right run, and the IMEI/REF parts are kept apart.
    assert "\u202aHonor X50 / X70\u202c" in html
    assert "\u202aIMEI 356789123456789\u202c" in html
    assert "\u200f&nbsp;·&nbsp;\u200f" in html


def test_cells_are_written_right_to_left_in_arabic(qapp) -> None:
    row = pdf_forms._tr("<td>first</td>", "<td>second</td>")
    assert row.index("second") < row.index("first")  # Qt lays cells out left to right


def test_a_twelve_payment_commitment_fits_one_a4_page(qapp, tmp_path: Path) -> None:
    from PySide6.QtPdf import QPdfDocument

    customer, sale = _twelve_payments()
    templates = print_templates.default_templates()
    templates = replace(templates, commitment_clauses=templates.commitment_clauses * 2)

    output = pdf_forms.generate_commitment_pdf(tmp_path / "commitment", customer, sale, templates=templates)

    document = QPdfDocument(None)
    document.load(str(output))
    assert output.suffix == ".pdf" and document.pageCount() == 1


def test_receipts_list_each_product_and_use_the_footer_lines(qapp, tmp_path: Path) -> None:
    templates = replace(print_templates.default_templates(), receipt_footer_lines=("لا ترد السلعة بعد 3 أيام",))
    items = [pdf_forms.ReceiptItem("Redmi 13", 1, 32_000), pdf_forms.ReceiptItem("Case", 2, 3_000)]
    html = pdf_forms._cash_receipt_html(
        "زبون", None, "", "cash", 35_000, 35_000, 0, date(2026, 10, 4), None, "file:///logo.png", "seller",
        items=items, receipt_number=12, templates=templates,
    )

    assert "Redmi 13" in html and "Case ×2" in html and "000012" in html
    assert "لا ترد السلعة بعد 3 أيام" in html and "seller" not in html
    output = pdf_forms.generate_cash_sale_receipt_pdf(
        tmp_path / "receipt.pdf", "زبون", None, "", "cash", 35_000, 35_000, 0, date(2026, 10, 4),
        items=items, receipt_number=12, templates=templates,
    )
    assert output.stat().st_size > 1000


def test_the_payment_receipt_shows_the_sale_balance_after_the_payment(qapp) -> None:
    customer, sale, payment = pdf_forms._sample_documents()
    details = pdf_forms._receipt_payment_details(sale, payment)

    assert details["balance_after"] == 0  # this installment is paid
    assert details["sale_balance_after"] == sale.financed - payment.amount


@pytest.mark.parametrize("kind", pdf_forms.PREVIEW_KINDS)
def test_previews_render_every_document(qapp, kind: str, tmp_path: Path) -> None:
    templates = print_templates.default_templates()
    image = pdf_forms.preview_image(kind, templates, 420)
    assert not image.isNull() and image.width() == 420 and image.height() > 420

    output = pdf_forms.write_preview_pdf(kind, templates, tmp_path / f"{kind}.pdf")
    assert output.stat().st_size > 1000


# ------------------------------------------------- the shop's NIS and NIF
NIS = "099810000123456"
NIF = "000016001234567"


def _shop_templates() -> PrintTemplates:
    """The print templates of a shop that filled in its three identifiers."""
    return replace(
        print_templates.default_templates(),
        business_nis=NIS, business_nif=NIF, business_rc="16/00-1234567",
    )


def _ids(html: str) -> str:
    return html[html.index("class='ids'") : html.index("</div>", html.index("class='ids'"))] if "class='ids'" in html else ""


def test_the_convention_prints_the_business_nis_and_nif(qapp) -> None:
    customer, sale, _payment = pdf_forms._sample_documents()

    html = pdf_forms.commitment_html(customer, sale, _shop_templates(), "file:///logo.png")

    assert f"NIS : {NIS}" in html and f"NIF : {NIF}" in html
    assert "RC : 16/00-1234567" in html and "AI : " not in html  # the three identifiers of the footer, no AI
    # The identifiers sit at the foot of the form, under the signatures (owner's choice 2026-10-06).
    assert html.index(f"NIS : {NIS}") > html.index("class='signature'")
    assert _ids(html) == ""


def test_the_taahud_and_the_iltizam_print_the_business_nis_and_nif(qapp, tmp_path: Path) -> None:
    # "تعهد والتزام" is one form: the pledge and the undertaking are the same document.
    customer, sale, _payment = pdf_forms._sample_documents()
    templates = _shop_templates()

    output = pdf_forms.generate_commitment_pdf(tmp_path / "commitment.pdf", customer, sale, templates=templates)
    html = pdf_forms.commitment_html(customer, sale, templates, "file:///logo.png")

    assert f"NIS : {NIS}" in html and f"NIF : {NIF}" in html
    assert output.stat().st_size > 1000
    # An explicit value still wins over the saved one (the sale receipt's rule).
    overridden = pdf_forms.commitment_html(
        customer, sale, templates, "file:///logo.png", business_nis="111", business_nif="222",
    )
    assert "NIS : 111" in overridden and "NIS : 222" not in overridden


def test_the_ticket_prints_the_business_nis_and_nif(qapp, tmp_path: Path) -> None:
    templates = _shop_templates()
    items = [pdf_forms.ReceiptItem("Redmi 13", 1, 32_000)]

    html = pdf_forms._cash_receipt_html(
        "زبون", None, "", "cash", 35_000, 35_000, 0, date(2026, 10, 4), None, "file:///logo.png", "seller",
        items=items, receipt_number=12, templates=templates,
    )
    output = pdf_forms.generate_cash_sale_receipt_pdf(
        tmp_path / "ticket.pdf", "زبون", None, "", "cash", 35_000, 35_000, 0, date(2026, 10, 4),
        items=items, receipt_number=12, templates=templates,
    )

    assert f"NIS: {NIS}" in html and f"NIF: {NIF}" in html
    assert output.stat().st_size > 1000


def test_the_payment_receipt_prints_the_business_nis_and_nif(qapp, tmp_path: Path) -> None:
    customer, sale, payment = pdf_forms._sample_documents()
    templates = _shop_templates()
    details = pdf_forms._receipt_payment_details(sale, payment)

    html = pdf_forms._receipt_html(
        customer, sale, payment, details, "file:///logo.png", "seller", templates,
    )
    output = pdf_forms.generate_payment_receipt_pdf(
        tmp_path / "receipt.pdf", customer, sale, payment, issued_by="seller", templates=templates,
    )

    assert f"NIS: {NIS}" in html and f"NIF: {NIF}" in html
    ids = _ids(html)
    assert f"NIS: {NIS}" in ids and f"NIF: {NIF}" in ids  # the line sits in the document header
    assert output.stat().st_size > 1000


def test_documents_without_identifiers_keep_no_empty_ids_line(qapp) -> None:
    customer, sale, payment = pdf_forms._sample_documents()
    templates = print_templates.default_templates()  # a shop that filled nothing in
    details = pdf_forms._receipt_payment_details(sale, payment)

    commitment = pdf_forms.commitment_html(customer, sale, templates, "file:///logo.png")
    receipt = pdf_forms._receipt_html(customer, sale, payment, details, "file:///logo.png", None, templates)

    # The commitment always ends with the three identifiers, blank ones as dotted lines to fill in.
    assert "class='ids'" not in commitment
    for label in ("RC", "NIS", "NIF"):
        assert f"{label} : <span" in commitment
    assert "AI : " not in commitment
    assert "class='ids'" not in receipt and "NIS" not in receipt
