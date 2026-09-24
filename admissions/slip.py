"""
The application slip: what a family keeps to find their application again.

It carries the application number and the private link, as text and as a QR code, so a
phone camera reopens the family's page. The link is never stored; the slip is made only
while the raw link is in hand, right after applying or after the office gives a new one.
"""

from xml.sax.saxutils import escape

from django.utils import formats, timezone
from django.utils.translation import gettext
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from core.pdf import document, styles
from core.qr import qr_drawing


def slip_pdf(application, link):
    style = styles()
    round_class = application.round_class
    admission_round = round_class.admission_round
    rows = [
        (gettext("Application number"), application.reference),
        (gettext("Child"), application.child_name),
        (gettext("Class applied for"), f"{round_class.class_level} · {admission_round}"),
        (
            gettext("Applied on"),
            formats.date_format(timezone.localtime(application.submitted_at).date(), "j M Y"),
        ),
    ]
    facts = Table(
        [
            [Paragraph(escape(str(label)), style["cell"]), Paragraph(f"<b>{escape(str(value))}</b>", style["cell"])]
            for label, value in rows
        ],
        colWidths=[45 * mm, None],
    )
    facts.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    link_block = Table(
        [
            [
                qr_drawing(link, size_mm=34),
                [
                    Paragraph(f"<b>{escape(gettext('Your private link'))}</b>", style["normal"]),
                    Spacer(1, 3),
                    Paragraph(f'<font face="Courier" size="8">{escape(link)}</font>', style["normal"]),
                    Spacer(1, 6),
                    Paragraph(
                        escape(
                            gettext(
                                "Scan the code or open the link to see where the application stands, "
                                "upload documents and answer an offer."
                            )
                        ),
                        style["normal"],
                    ),
                ],
            ]
        ],
        colWidths=[40 * mm, None],
    )
    link_block.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    flow = [facts, Spacer(1, 8), link_block, Spacer(1, 10)]
    for line in (
        gettext("Keep this slip safe. Anyone with the link can see the application, so do not share it."),
        gettext("If you lose it, contact the school office with the application number for a new one."),
    ):
        flow.append(Paragraph(escape(line), style["normal"]))
        flow.append(Spacer(1, 3))
    return document(
        application.school,
        gettext("Application slip"),
        flow,
        filename=f"{application.reference}.pdf",
        as_attachment=False,
    )
