"""The money receipt for an application fee paid at the office."""

from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from core.money import amount_in_words
from core.pdf import document, styles
from fees.documents import _facts


def receipt_pdf(payment):
    style = styles()
    application = payment.application
    school = payment.school
    currency = school.currency_symbol
    flow = [
        _facts(
            [
                ("Receipt no", escape(payment.receipt_no)),
                ("Date", f"{payment.date:%d %b %Y}"),
                ("Application", escape(application.reference)),
                ("Child", escape(application.child_name)),
                ("Class applied for", escape(f"{application.round_class.class_level}")),
                ("Payment method", escape(payment.get_method_display())),
            ],
            style,
        ),
        Spacer(1, 8),
        Paragraph(f"Application fee received: <b>{escape(currency)}{payment.amount}</b>", style["normal"]),
        Paragraph(f"In words: <b>{escape(amount_in_words(payment.amount))}</b>", style["normal"]),
    ]
    if payment.reference:
        flow.append(Paragraph(f"Reference: {escape(payment.reference)}", style["normal"]))
    if payment.is_voided:
        flow.append(Spacer(1, 6))
        flow.append(
            Paragraph(f"<font color='#b91c1c'><b>VOID</b> — {escape(payment.void_reason)}</font>", style["normal"])
        )
    flow.append(Spacer(1, 16))
    signature = Table(
        [
            [
                Paragraph(
                    f"<font color='#475569'>Received by {escape(str(payment.received_by or ''))}</font>", style["cell"]
                ),
                Paragraph("<font color='#475569'>Authorised signature</font>", style["right"]),
            ]
        ],
        colWidths=[89 * mm, 89 * mm],
    )
    signature.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (0, 0), 0.5, colors.black),
                ("LINEABOVE", (1, 0), (1, 0), 0.5, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    flow.append(signature)
    return document(school, "Application fee receipt", flow, filename=f"{payment.receipt_no}.pdf", as_attachment=False)
