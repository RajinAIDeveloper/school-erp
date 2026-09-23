"""
Printed examination documents: report cards, admit cards and the exam routine.

A report card leaves the school and is kept by a family for years, so it carries the
school's letterhead, the student's own name (in Bangla too when the school records one),
the marks as published, and a verification code that can be checked later.
"""

from decimal import Decimal
from io import BytesIO
from xml.sax.saxutils import escape

from django.db.models import Count, Q
from django.http import HttpResponse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core.pdf import HEADER_FILL, RULE, data_table, document, letterhead, styles


def _facts(pairs, style, columns=3, width=178):
    cells = [
        Paragraph(
            f"<font color='#475569' size='7.5'>{escape(str(label))}</font><br/><b>{escape(str(value))}</b>",
            style["cell"],
        )
        for label, value in pairs
    ]
    rows = [cells[i : i + columns] for i in range(0, len(cells), columns)]
    for row in rows:
        while len(row) < columns:
            row.append("")
    table = Table(rows, colWidths=[(width / columns) * mm] * columns)
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return table


def attendance_for(enrollment):
    """Attendance across the exam's academic year, for the card's summary line."""
    from attendance.models import StudentAttendance

    counts = StudentAttendance.objects.filter(enrollment=enrollment).aggregate(
        total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"]))
    )
    if not counts["total"]:
        return None
    return {
        "total": counts["total"],
        "present": counts["present"],
        "percent": round(counts["present"] * 100 / counts["total"]),
    }


def report_card_flowables(school, exam, enrollment, row, snapshot, style, verify_url=""):
    student = enrollment.student
    attendance = attendance_for(enrollment)
    facts = [
        ("Student", student.full_name),
        ("Student ID", student.student_id),
        ("Class / section", str(enrollment.section)),
        ("Roll", enrollment.roll_number),
        ("Session", str(exam.academic_year)),
        ("Examination", exam.name),
    ]
    if student.name_bn:
        facts.insert(1, ("নাম", student.name_bn))
    if attendance:
        facts.append(("Attendance", f"{attendance['percent']}% ({attendance['present']}/{attendance['total']})"))

    subject_rows = []
    for cell in row["cells"]:
        obtained = "ABS" if cell["absent"] else ("—" if cell["missing"] else cell["score"])
        subject_rows.append(
            [
                cell["subject"],
                cell["full_marks"],
                cell["pass_marks"],
                obtained,
                cell["letter"] if not cell["missing"] else "—",
                cell["grade_point"] if not cell["missing"] else "—",
            ]
        )
    table = data_table(
        ["Subject", "Full marks", "Pass marks", "Obtained", "Grade", "Points"],
        subject_rows,
        style,
        align_right=(1, 2, 3),
    )

    result_style = "#047857" if row["result"] == "PASS" else "#b91c1c"
    summary = Table(
        [
            [
                Paragraph(
                    f"<font color='#475569' size='7.5'>Total</font><br/><b>{row['total']} / {row['full_total']}</b>",
                    style["cell"],
                ),
                Paragraph(
                    f"<font color='#475569' size='7.5'>Percentage</font><br/><b>{row['percent'] or '—'}%</b>",
                    style["cell"],
                ),
                Paragraph(f"<font color='#475569' size='7.5'>GPA</font><br/><b>{row['gpa'] or '—'}</b>", style["cell"]),
                Paragraph(
                    f"<font color='#475569' size='7.5'>Rank in section</font><br/><b>{row['rank'] or '—'}</b>",
                    style["cell"],
                ),
                Paragraph(
                    f"<font color='#475569' size='7.5'>Result</font><br/>"
                    f"<b><font color='{result_style}'>{row['result']}</font></b>",
                    style["cell"],
                ),
            ]
        ],
        colWidths=[35.6 * mm] * 5,
    )
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HEADER_FILL),
                ("BOX", (0, 0), (-1, -1), 0.4, RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    signatures = Table(
        [
            [
                Paragraph("<font color='#475569' size='7.5'>Class teacher</font>", style["cell"]),
                Paragraph("<font color='#475569' size='7.5'>Guardian</font>", style["cell"]),
                Paragraph(
                    f"<font color='#475569' size='7.5'>{escape(school.principal_name or 'Head of institution')}</font>",
                    style["cell"],
                ),
            ]
        ],
        colWidths=[59 * mm] * 3,
    )
    signatures.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    flow = [_facts(facts, style), Spacer(1, 6), table, Spacer(1, 8), summary, Spacer(1, 6)]
    if exam.status != "published":
        flow.append(
            Paragraph("<font color='#b91c1c'><b>DRAFT</b> — these results are not published.</font>", style["normal"])
        )
    if snapshot:
        where = escape(verify_url) if verify_url else "the school's report verification page"
        flow.append(
            Paragraph(
                f"<font color='#475569' size='7.5'>Version {exam.publication_version} · "
                f"verify this card at {where}</font>",
                style["cell"],
            )
        )
    flow.append(Spacer(1, 18))
    flow.append(signatures)
    return flow


def report_card_pdf(school, exam, enrollment, row, snapshot, verify_url=""):
    style = styles()
    return document(
        school,
        f"Report card · {exam.name}",
        report_card_flowables(school, exam, enrollment, row, snapshot, style, verify_url),
        subtitle=f"{enrollment.student.full_name} · {enrollment.section}",
        filename=f"report-card-{enrollment.student.student_id}.pdf",
    )


def bulk_report_cards_pdf(school, exam, cards):
    """One PDF holding a card per student, ready for the printer."""
    style = styles()
    flow = []
    for index, card in enumerate(cards):
        enrollment, row, snapshot = card[0], card[1], card[2]
        verify_url = card[3] if len(card) > 3 else ""
        if index:
            flow.append(PageBreak())
        flow.extend(
            letterhead(
                school, f"Report card · {exam.name}", f"{enrollment.student.full_name} · {enrollment.section}", style
            )
        )
        flow.extend(report_card_flowables(school, exam, enrollment, row, snapshot, style, verify_url))
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Report cards · {exam.name}",
    )
    doc.build(flow or [Paragraph("No students to print.", style["normal"])])
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="report-cards.pdf"'
    return response


def admit_cards_pdf(school, exam, enrollments, schedules):
    """One admit card per student, each listing the papers they will sit."""
    style = styles()
    paper_rows = [
        [
            s.subject.name,
            s.date.strftime("%d %b %Y") if s.date else "To be announced",
            f"{s.start_time:%H:%M}" if s.start_time else "",
            s.room or "",
        ]
        for s in schedules
    ]
    flow = []
    for index, enrollment in enumerate(enrollments):
        if index:
            flow.append(PageBreak())
        student = enrollment.student
        flow.extend(letterhead(school, f"Admit card · {exam.name}", str(exam.academic_year), style))
        flow.append(
            _facts(
                [
                    ("Student", student.full_name),
                    ("Student ID", student.student_id),
                    ("Class / section", str(enrollment.section)),
                    ("Roll", enrollment.roll_number),
                    ("Examination", exam.name),
                    ("Starts", exam.start_date.strftime("%d %b %Y") if exam.start_date else "To be announced"),
                ],
                style,
            )
        )
        flow.append(Spacer(1, 6))
        flow.append(data_table(["Subject", "Date", "Time", "Room"], paper_rows, style))
        flow.append(Spacer(1, 10))
        flow.append(
            Paragraph(
                "<font color='#475569' size='8'>Bring this card to every paper. Candidates must be seated "
                "ten minutes before the start.</font>",
                style["cell"],
            )
        )
        flow.append(Spacer(1, 16))
        signature = Table(
            [[Paragraph("<font color='#475569' size='7.5'>Controller of examinations</font>", style["cell"])]],
            colWidths=[70 * mm],
            hAlign="RIGHT",
        )
        signature.setStyle(
            TableStyle([("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.black), ("TOPPADDING", (0, 0), (-1, -1), 4)])
        )
        flow.append(signature)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"Admit cards · {exam.name}",
    )
    doc.build(flow or [Paragraph("No students to print.", style["normal"])])
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="admit-cards.pdf"'
    return response


def progress_rows(exams, enrollments_by_year):
    """GPA per published exam for one student, oldest first."""
    from .services import build_result_sheet

    rows = []
    for exam in exams:
        enrollment = enrollments_by_year.get(exam.academic_year_id)
        if enrollment is None:
            continue
        sheet = build_result_sheet(exam, enrollment.class_level, enrollment.section)
        row = next((r for r in sheet["rows"] if r["enrollment_id"] == enrollment.pk), None)
        if row is None:
            continue
        rows.append(
            {
                "exam": exam,
                "section": str(enrollment.section),
                "total": Decimal(row["total"]),
                "percent": row["percent"],
                "gpa": row["gpa"],
                "result": row["result"],
                "rank": row["rank"],
            }
        )
    return rows
