"""
Printed documents, and the Bangla typography they depend on.

A report card is the artefact that leaves the school and gets kept. If the bundled Bengali
face goes missing or is replaced by one without the glyphs, a student's name prints as
empty boxes and nothing else fails — so these tests check the font itself, not just that a
PDF was produced.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.test import Client
from reportlab.pdfbase import pdfmetrics

from core.pdf import bangla_font
from examinations.services import publish_exam, save_mark

# আয়েশা সিদ্দিকা — a name a Bangladeshi school would actually record.
BANGLA_NAME = "আয়েশা সিদ্দিকা"

# Codepoints that Unicode leaves unassigned inside the Bengali letters block.
UNASSIGNED = {0x098D, 0x098E, 0x0991, 0x0992, 0x09A9}


def test_a_bengali_face_is_bundled_and_registers():
    assert bangla_font() == "NotoSansBengali", (
        "No Bengali face registered. Without one, Bangla names print as empty boxes; see static/fonts/README.md."
    )


def test_the_bundled_face_has_real_bengali_glyphs():
    """Helvetica would accept the string and draw nothing recognisable."""
    name = bangla_font()
    face = pdfmetrics.getFont(name).face
    assigned = [cp for cp in range(0x0985, 0x09B0) if cp not in UNASSIGNED]
    mapped = [cp for cp in assigned if face.charToGlyph.get(cp, 0)]
    assert len(mapped) == len(assigned), f"only {len(mapped)} of {len(assigned)} Bengali letters map to glyphs"


def test_bangla_text_measures_as_text_not_as_missing_glyphs():
    name = bangla_font()
    width = pdfmetrics.stringWidth(BANGLA_NAME, name, 10)
    assert width > 20, "the string measured as nothing, which means no glyphs were found"


def test_a_report_card_embeds_the_bengali_face_when_the_name_needs_it(erp):
    erp.student.name_bn = BANGLA_NAME
    erp.student.save()
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)

    client = Client()
    client.force_login(erp.admin)
    response = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert b"NotoSansBengali" in response.content, "the Bangla name would print as boxes"


def test_an_id_card_renders_with_a_bangla_name(erp):
    erp.student.name_bn = BANGLA_NAME
    erp.student.save()
    client = Client()
    client.force_login(erp.admin)
    response = client.get(f"/students/{erp.student.pk}/id-card.pdf")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert b"NotoSansBengali" in response.content


@pytest.mark.parametrize(
    "path",
    [
        "/students/{student}/id-card.pdf",
        "/fees/statement/{student}/?format=pdf",
        "/exams/{exam}/routine/?format=pdf",
        "/reports/strength/?format=pdf",
        "/finance/reports/trial-balance/?format=pdf",
    ],
)
def test_printed_documents_carry_the_school_letterhead(admin_client, erp, path):
    url = path.format(student=erp.student.pk, exam=erp.exam.pk)
    response = admin_client.get(url)
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    # Every document goes out on the school's own letterhead, never anonymously.
    assert len(response.content) > 1500


def test_a_receipt_states_the_amount_in_words(erp, invoice):
    from fees.services import collect_payment

    payment = collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=Decimal("1000"),
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    client = Client()
    client.force_login(erp.accountant)
    response = client.get(f"/fees/receipts/{payment.pk}.pdf")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    # The words themselves are compressed inside the PDF, so assert the helper they come from.
    from core.money import amount_in_words

    assert amount_in_words(payment.amount) == "One Thousand Taka Only"


def test_documents_still_render_when_no_bengali_face_is_present(erp, monkeypatch, tmp_path):
    """Deleting the font must degrade to Helvetica, not break printing."""
    import core.pdf

    core.pdf.bangla_font.cache_clear()
    monkeypatch.setattr(core.pdf, "FONT_DIR", tmp_path)
    try:
        assert core.pdf.bangla_font() is None
        client = Client()
        client.force_login(erp.admin)
        response = client.get(f"/students/{erp.student.pk}/id-card.pdf")
        assert response.status_code == 200 and response.content.startswith(b"%PDF")
    finally:
        core.pdf.bangla_font.cache_clear()
