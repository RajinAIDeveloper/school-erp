"""Printable fee documents: money receipts and student statements."""

from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from core.money import amount_in_words
from core.pdf import HEADER_FILL, MUTED, RULE, data_table, document, styles

ZERO = Decimal("0.00")


def _facts(pairs, style, columns=2):
    """A compact label/value block, two columns wide by default."""
    cells = []
    for label, value in pairs:
        cells.append(Paragraph(f"<font color='#475569'>{label}</font><br/><b>{value}</b>", style["cell"]))
    rows = [cells[i : i + columns] for i in range(0, len(cells), columns)]
    for row in rows:
        while len(row) < columns:
            row.append("")
    table = Table(rows, colWidths=[(178 / columns) * mm] * columns)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def receipt_pdf(school, payment, *, copy=False):
    """A money receipt the family can keep and the office can reconcile."""
    style = styles()
    invoice = payment.invoice
    student = invoice.student
    enrollment = invoice.enrollment
    currency = school.currency_symbol

    flow = [
        _facts(
            [
                ("Receipt no", payment.receipt_no),
                ("Date", f"{payment.date:%d %b %Y}"),
                ("Student", f"{student.full_name} ({student.student_id})"),
                ("Class / roll", f"{enrollment.section} · {enrollment.roll_number}"),
                ("Invoice", invoice.invoice_no),
                ("Payment method", payment.get_method_display()),
            ],
            style,
        ),
        Spacer(1, 6),
    ]

    items = [
        [item.category.name, item.description or "", f"{currency}{item.amount}"]
        for item in invoice.items.select_related("category")
    ]
    if invoice.vat_total:
        items.append(["VAT", "", f"{currency}{invoice.vat_total}"])
    if invoice.discount:
        items.append(["Discount", "", f"-{currency}{invoice.discount}"])
    if invoice.late_fee:
        items.append(["Late fee", "", f"{currency}{invoice.late_fee}"])
    items.append(["Invoice total", "", f"{currency}{invoice.total}"])
    table = data_table(["Fee head", "Details", "Amount"], items, style, align_right=(2,))
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, len(items)), (-1, len(items)), HEADER_FILL),
                ("FONTNAME", (0, len(items)), (-1, len(items)), "Helvetica-Bold"),
            ]
        )
    )
    flow.append(table)
    flow.append(Spacer(1, 8))

    balance = invoice.balance
    summary = Table(
        [
            [
                Paragraph("<b>Amount received</b>", style["cell"]),
                Paragraph(f"<b>{currency}{payment.amount}</b>", style["right"]),
            ],
            [Paragraph("Balance after this receipt", style["cell"]), Paragraph(f"{currency}{balance}", style["right"])],
        ],
        colWidths=[120 * mm, 58 * mm],
    )
    summary.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, RULE),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    flow.append(summary)
    flow.append(Spacer(1, 4))
    flow.append(Paragraph(f"In words: <b>{amount_in_words(payment.amount)}</b>", style["normal"]))

    if payment.reference:
        flow.append(Paragraph(f"Reference: {payment.reference}", style["normal"]))
    if payment.is_cancelled:
        flow.append(Spacer(1, 6))
        flow.append(
            Paragraph(f"<font color='#b91c1c'><b>CANCELLED</b> — {payment.cancel_reason}</font>", style["normal"])
        )

    flow.append(Spacer(1, 16))
    signature = Table(
        [
            [
                Paragraph(f"<font color='#475569'>Received by {payment.received_by or ''}</font>", style["cell"]),
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

    title = "Money receipt" + (" (duplicate copy)" if copy else "")
    return document(
        school,
        title,
        flow,
        subtitle=f"Receipt {payment.receipt_no}",
        filename=f"receipt-{payment.receipt_no}.pdf",
    )


def statement_pdf(school, student, invoices, payments, *, period=""):
    """Every charge and every receipt for one student, with a running balance."""
    style = styles()
    currency = school.currency_symbol
    events = []
    for invoice in invoices:
        events.append((invoice.issue_date, f"Invoice {invoice.invoice_no}", invoice.total, ZERO))
    for payment in payments:
        label = f"Receipt {payment.receipt_no}" + (" (cancelled)" if payment.is_cancelled else "")
        events.append((payment.date, label, ZERO, ZERO if payment.is_cancelled else payment.amount))
    events.sort(key=lambda row: (row[0], row[1]))

    running = ZERO
    rows = []
    for when, label, charge, paid in events:
        running += charge - paid
        rows.append(
            [
                f"{when:%d %b %Y}",
                label,
                f"{currency}{charge}" if charge else "",
                f"{currency}{paid}" if paid else "",
                f"{currency}{running}",
            ]
        )

    enrollment = student.current_enrollment
    flow = [
        _facts(
            [
                ("Student", f"{student.full_name} ({student.student_id})"),
                ("Class / roll", f"{enrollment.section} · {enrollment.roll_number}" if enrollment else "Not enrolled"),
                ("Guardian", getattr(student.primary_guardian, "full_name", "—")),
                ("Closing balance", f"{currency}{running}"),
            ],
            style,
        ),
        Spacer(1, 6),
        data_table(["Date", "Particulars", "Charged", "Received", "Balance"], rows, style, align_right=(2, 3, 4)),
        Spacer(1, 8),
        Paragraph(f"Closing balance in words: <b>{amount_in_words(running)}</b>", style["normal"]),
        Spacer(1, 4),
        Paragraph(
            "<font color='#475569'>A positive balance is owed to the school. Cancelled receipts are listed "
            "for completeness and are not counted.</font>",
            style["cell"],
        ),
    ]
    return document(
        school,
        "Fee statement",
        flow,
        subtitle=f"{student.full_name} · {period}".strip(" ·"),
        filename=f"statement-{student.student_id}.pdf",
    )


__all__ = ["receipt_pdf", "statement_pdf", "MUTED"]
