"""WhatsApp reminder link formatting."""

from app.services.reminders import international_number, whatsapp_link


def test_local_mobile_numbers_get_the_algerian_country_code() -> None:
    assert international_number("0661234567") == "213661234567"
    assert international_number("0661 23 45 67") == "213661234567"
    assert international_number("٠٦٦١٢٣٤٥٦٧") == "213661234567"


def test_international_forms_are_kept() -> None:
    assert international_number("+213661234567") == "213661234567"
    assert international_number("00213661234567") == "213661234567"


def test_missing_or_short_numbers_have_no_link() -> None:
    assert international_number(None) is None
    assert international_number("") is None
    assert international_number("123") is None
    assert whatsapp_link(None, "hello") is None


def test_link_encodes_the_drafted_message() -> None:
    link = whatsapp_link("0661234567", "السلام عليكم 1,000 دج")
    assert link is not None
    assert link.startswith("https://wa.me/213661234567?text=")
    assert " " not in link
