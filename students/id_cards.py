"""Printable student identity cards, laid out two per row on A4."""

from io import BytesIO
from xml.sax.saxutils import escape

from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core.pdf import INK, MUTED, RULE, letterhead, styles

CARD_WIDTH = 86 * mm
CARD_HEIGHT = 54 * mm


def _card(student, school, style):
    """One card as a nested table: photo on the left, details on the right."""
    enrollment = student.current_enrollment
    guardian = student.primary_guardian
    lines = [
        f"<b>{escape(student.full_name)}</b>",
        escape(student.name_bn) if student.name_bn else "",
        f"ID {escape(student.student_id)}",
        escape(f"{enrollment.section} · Roll {enrollment.roll_number}") if enrollment else "Not enrolled",
        escape(f"Session {enrollment.academic_year}") if enrollment else "",
        f"Blood {escape(student.blood_group)}" if student.blood_group else "",
        escape(f"Guardian {guardian.phone}") if guardian and guardian.phone else "",
    ]
    details = [[Paragraph(line, style["cell"])] for line in lines if line]

    photo = []
    if student.photo:
        try:
            photo = [Image(student.photo.path, width=20 * mm, height=24 * mm)]
        except Exception:  # noqa: BLE001 - a missing photo must not stop the print run
            photo = []
    if not photo:
        photo = [Paragraph("<font color='#94a3b8'>No photo</font>", style["cell"])]

    inner = Table(
        [[photo, Table(details, colWidths=[CARD_WIDTH - 34 * mm])]],
        colWidths=[24 * mm, CARD_WIDTH - 30 * mm],
    )
    inner.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 3)]))

    header = Paragraph(f"<b>{escape(school.short_name or school.name)}</b>", style["cell"])
    card = Table([[header], [inner]], colWidths=[CARD_WIDTH], rowHeights=[8 * mm, CARD_HEIGHT - 8 * mm])
    card.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, RULE),
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#e0e7ff")),
                ("TEXTCOLOR", (0, 0), (0, 0), INK),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("LINEBELOW", (0, 0), (0, 0), 0.4, MUTED),
            ]
        )
    )
    return card


def id_card_pdf(school, students, subtitle=""):
    style = styles()
    cards = [_card(student, school, style) for student in students]
    rows = [cards[i : i + 2] for i in range(0, len(cards), 2)]
    for row in rows:
        while len(row) < 2:
            row.append("")
    grid = Table(rows, colWidths=[CARD_WIDTH + 4 * mm] * 2, hAlign="LEFT") if rows else Spacer(1, 1)
    if rows:
        grid.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title="Student identity cards",
    )
    title = "Student identity card" if len(students) == 1 else "Student identity cards"
    doc.build(letterhead(school, title, subtitle, style) + [grid])
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="id-cards.pdf"'
    return response
