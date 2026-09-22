"""
Shared PDF furniture: a school letterhead, Bangla-capable fonts and a table style,
so every printed document in the ERP looks like it came from the same office.
"""

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

FONT_DIR = Path(settings.BASE_DIR) / "static" / "fonts"
BANGLA_FONT = "NotoSansBengali"
INK = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#475569")
RULE = colors.HexColor("#cbd5e1")
HEADER_FILL = colors.HexColor("#e2e8f0")


@lru_cache(maxsize=1)
def bangla_font():
    """
    Register a Unicode Bengali face when one is bundled. Latin-only fonts render Bangla
    names as blanks, which is worse than useless on a report card, so callers fall back
    to Helvetica only when the file is genuinely absent.
    """
    candidates = sorted(FONT_DIR.glob("NotoSansBengali*.ttf")) if FONT_DIR.exists() else []
    if not candidates:
        return None
    try:
        pdfmetrics.registerFont(TTFont(BANGLA_FONT, str(candidates[0])))
    except Exception:  # noqa: BLE001 - a broken font file must not break printing
        return None
    return BANGLA_FONT


def styles():
    base = getSampleStyleSheet()
    font = bangla_font()
    body_font = font or "Helvetica"
    return {
        "title": ParagraphStyle(
            "erp-title",
            parent=base["Title"],
            fontName=font or "Helvetica-Bold",
            fontSize=16,
            spaceAfter=2,
            textColor=INK,
        ),
        "school": ParagraphStyle(
            "erp-school",
            parent=base["Normal"],
            fontName=font or "Helvetica-Bold",
            fontSize=13,
            alignment=1,
            textColor=INK,
        ),
        "muted": ParagraphStyle(
            "erp-muted", parent=base["Normal"], fontName=body_font, fontSize=8.5, alignment=1, textColor=MUTED
        ),
        "normal": ParagraphStyle("erp-normal", parent=base["Normal"], fontName=body_font, fontSize=9, textColor=INK),
        "cell": ParagraphStyle("erp-cell", parent=base["Normal"], fontName=body_font, fontSize=8.5, textColor=INK),
        "right": ParagraphStyle(
            "erp-right", parent=base["Normal"], fontName=body_font, fontSize=9, alignment=2, textColor=INK
        ),
    }


def letterhead(school, title, subtitle="", style=None):
    """School name, address and document title as flowables."""
    style = style or styles()
    flow = []
    logo = getattr(school, "logo", None)
    if logo:
        try:
            flow.append(Image(logo.path, width=18 * mm, height=18 * mm, kind="proportional"))
        except Exception:  # noqa: BLE001 - a missing logo file must not break the document
            pass
    flow.append(Paragraph(escape(school.name), style["school"]))
    line = " · ".join(
        part
        for part in [school.address.replace("\n", ", ") if school.address else "", school.phone, school.email]
        if part
    )
    if line:
        flow.append(Paragraph(escape(line), style["muted"]))
    if getattr(school, "eiin", ""):
        flow.append(Paragraph(f"EIIN {escape(school.eiin)}", style["muted"]))
    flow.append(Spacer(1, 7))
    flow.append(Paragraph(escape(title), style["title"]))
    if subtitle:
        flow.append(Paragraph(escape(subtitle), style["muted"]))
    flow.append(Spacer(1, 9))
    return flow


def data_table(headers, rows, style=None, align_right=()):
    style = style or styles()
    data = [[Paragraph(f"<b>{escape(str(h))}</b>", style["cell"]) for h in headers]]
    for row in rows:
        data.append(
            [
                Paragraph(escape("" if v is None else str(v)), style["right"] if i in align_right else style["cell"])
                for i, v in enumerate(row)
            ]
        )
    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), HEADER_FILL),
                ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    return table


def document(
    school, title, flowables, *, subtitle="", filename="document.pdf", landscape_mode=False, as_attachment=True
):
    """Render flowables under the school letterhead and return them as a PDF response."""
    buffer = BytesIO()
    page = landscape(A4) if landscape_mode else A4
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=title,
    )
    doc.build(letterhead(school, title, subtitle) + list(flowables))
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    disposition = "attachment" if as_attachment else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


def table_document(school, title, headers, rows, *, subtitle="", filename="report.pdf", align_right=()):
    """The common case: one titled table on school letterhead."""
    style = styles()
    return document(
        school,
        title,
        [data_table(headers, rows, style, align_right)],
        subtitle=subtitle,
        filename=filename,
        landscape_mode=len(headers) > 6,
    )
