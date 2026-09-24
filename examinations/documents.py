"""
Printed examination documents: report cards, admit cards and the exam routine.

A report card leaves the school and is kept by a family for years, so it carries the
school's letterhead, the student's own name (in Bangla too when the school records one),
the marks as published, and a verification code that can be checked later.
"""

from datetime import date
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

from .grading import headline, shows_rank
from .parts import plain


def mark_text(value):
    """A mark as people write it, 92 rather than 92.00; words such as ABS or Exempt pass through."""
    try:
        return plain(value)
    except (ArithmeticError, ValueError, TypeError):
        return value


def shows_points(row):
    """Only rulebooks with grade points print a Points column; a Cambridge or IB card has none."""
    return row.get("has_gpa", True)


def total_text(row):
    return f"{mark_text(row['total'])} / {mark_text(row['full_total'])}"


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


def attendance_for(enrollment, until=None):
    """
    Attendance in the exam's year up to the exam, for cards made from an older snapshot.

    Newer snapshots carry this figure themselves, frozen at publication, so a card printed
    next year shows what the card printed today showed.
    """
    from attendance.models import StudentAttendance

    records = StudentAttendance.objects.filter(enrollment=enrollment)
    if until is not None:
        records = records.filter(date__lte=until)
    counts = records.aggregate(total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"])))
    if not counts["total"]:
        return None
    return {
        "total": counts["total"],
        "present": counts["present"],
        "percent": round(counts["present"] * 100 / counts["total"]),
        "until": until.isoformat() if until else "",
    }


def attendance_text(attendance):
    """ "92% (184/200) to 12 Jun 2026": the figure and the date it runs to."""
    from datetime import date

    text = f"{attendance['percent']}% ({attendance['present']}/{attendance['total']})"
    if attendance.get("until"):
        text += f" to {date.fromisoformat(attendance['until']):%d %b %Y}"
    return text


def card_rows(row):
    """
    The subject table of a card: one line per paper, and for a subject examined in two
    papers a combined line carrying its grade. Works for older snapshots, which have papers
    but no combined subjects, by treating each paper as a subject.
    """
    cells = {cell["schedule_id"]: cell for cell in row["cells"]}
    notes = row.get("comments") or {}
    units = row.get("subjects") or [
        {
            "name": cell["subject"],
            "codes": [],
            "papers": [cell["schedule_id"]],
            "full_marks": cell["full_marks"],
            "score": cell["score"],
            "missing": cell["missing"],
            "absent": cell["absent"],
            "letter": cell["letter"],
            "grade_point": cell["grade_point"],
            "is_fourth": cell.get("is_fourth", False),
        }
        for cell in row["cells"]
    ]
    lines = []
    for unit in units:
        papers = [cells[pk] for pk in unit["papers"] if pk in cells]
        combined = len(papers) > 1
        for paper in papers:
            parts = " · ".join(
                f"{part['name']} {mark_text(part['score']) if part['score'] is not None else '—'}"
                for part in paper.get("components") or []
            )
            obtained = "ABS" if paper["absent"] else ("—" if paper["missing"] else mark_text(paper["score"]))
            note = notes.get(str(paper.get("subject_id"))) or {}
            lines.append(
                {
                    "effort": note.get("effort", ""),
                    "comment": note.get("comment", ""),
                    "code": paper.get("subject_code", ""),
                    "subject": paper["subject"] + (" (4th subject)" if paper.get("is_fourth") and not combined else ""),
                    "full_marks": mark_text(paper["full_marks"]),
                    "parts": parts,
                    "obtained": obtained,
                    "letter": "" if combined else ("—" if paper["missing"] else paper["letter"]),
                    "grade_point": "" if combined else ("—" if paper["missing"] else paper["grade_point"]),
                    "combined": False,
                }
            )
        if combined:
            obtained = "ABS" if unit["absent"] else ("—" if unit["missing"] else mark_text(unit["score"]))
            lines.append(
                {
                    "effort": "",
                    "comment": "",
                    "code": "",
                    "subject": f"{unit['name']} (both papers)" + (" (4th subject)" if unit.get("is_fourth") else ""),
                    "full_marks": mark_text(unit["full_marks"]),
                    "parts": "",
                    "obtained": obtained,
                    "letter": "—" if unit["missing"] else unit["letter"],
                    "grade_point": "—" if unit["missing"] else unit["grade_point"],
                    "combined": True,
                }
            )
    for cell in row["cells"]:
        if cell.get("exempt"):
            note = notes.get(str(cell.get("subject_id"))) or {}
            lines.append(
                {
                    "effort": note.get("effort", ""),
                    "comment": note.get("comment", ""),
                    "code": cell.get("subject_code", ""),
                    "subject": cell["subject"],
                    "full_marks": "—",
                    "parts": "",
                    "obtained": "Exempt",
                    "letter": "EX",
                    "grade_point": "",
                    "combined": False,
                }
            )
    return lines


def report_card_flowables(school, exam, enrollment, row, snapshot, style, verify_url=""):
    """
    One card. Every figure comes from `row`, which for a published exam is the snapshot: the
    name, class, roll, attendance and marks as they stood at publication.
    """
    from core.qr import qr_drawing

    attendance = row["attendance"] if "attendance" in row else attendance_for(enrollment, exam.end_date)
    board = row.get("system") == "national"
    facts = [
        ("Student", row["student"]),
        ("Student ID", row["student_code"]),
        ("Class / section", row["section"]),
        ("Roll", row["roll"]),
        ("Session", str(exam.academic_year)),
        ("Examination", exam.name),
    ]
    name_bn = row.get("student_bn", enrollment.student.name_bn)
    if name_bn:
        facts.insert(1, ("নাম", name_bn))
    if row.get("group"):
        facts.append(("Group", row["group"]))
    if row.get("father_name"):
        facts.append(("Father", row["father_name"]))
    if attendance:
        facts.append(("Attendance", attendance_text(attendance)))

    lines = card_rows(row)
    show_parts = any(line["parts"] for line in lines)
    show_effort = any(line["effort"] for line in lines)
    show_points = shows_points(row)
    effort_heading = row.get("effort_label") or "Effort"
    headers = (
        ["Code", "Subject", "Full marks"]
        + (["Parts"] if show_parts else [])
        + ["Obtained", "Grade"]
        + (["Points"] if show_points else [])
        + ([effort_heading] if show_effort else [])
    )
    body = [
        [line["code"], line["subject"], line["full_marks"]]
        + ([line["parts"]] if show_parts else [])
        + [line["obtained"], line["letter"]]
        + ([line["grade_point"]] if show_points else [])
        + ([line["effort"]] if show_effort else [])
        for line in lines
    ]
    obtained_at = 4 if show_parts else 3
    numeric = (2, obtained_at) + ((obtained_at + 2,) if show_points else ())
    table = data_table(headers, body, style, align_right=numeric)
    words = [
        Paragraph(f"<b>{escape(line['subject'])}</b>: {escape(line['comment'])}", style["cell"])
        for line in lines
        if line["comment"]
    ]
    if row.get("overall_comment"):
        words.append(Paragraph(f"<b>Class teacher</b>: {escape(row['overall_comment'])}", style["cell"]))
    forecasts = row.get("forecasts") or []
    forecast_table = None
    if forecasts:
        forecast_table = data_table(
            ["Subject", "Kind", "Grade", "Decided"],
            [
                [f["subject"], f["kind_label"], f["grade"], f"{date.fromisoformat(f['as_of']):%d %b %Y}"]
                for f in forecasts
            ],
            style,
        )

    result_colour = "#047857" if row.get("result") == "PASS" else "#b91c1c"
    summary_cells = [("Total", total_text(row))]
    if row.get("has_gpa", True) and row.get("gpa") is not None:
        summary_cells.append(("GPA", f"{row['gpa']}" + (f" ({row['gpa_letter']})" if row.get("gpa_letter") else "")))
    elif row.get("points") is not None:
        summary_cells.append(("Points", row["points"]))
    else:
        summary_cells.append(("Grades", headline(row)))
    if board and row.get("fourth_subject"):
        summary_cells.append(("GPA without 4th subject", row.get("gpa_without_fourth") or "—"))
    if shows_rank(row):
        summary_cells.append(("Rank in section", row["rank"] or "—"))
    if row.get("result"):
        summary_cells.append(("Result", row["result"]))
    width = 178 / len(summary_cells)
    summary = Table(
        [
            [
                Paragraph(
                    f"<font color='#475569' size='7.5'>{escape(label)}</font><br/>"
                    + (
                        f"<b><font color='{result_colour}'>{escape(str(value))}</font></b>"
                        if label == "Result"
                        else f"<b>{escape(str(value))}</b>"
                    ),
                    style["cell"],
                )
                for label, value in summary_cells
            ]
        ],
        colWidths=[width * mm] * len(summary_cells),
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
    from .rulebooks import official_notice

    if row.get("sources"):
        made_of = ", ".join(f"{s['exam']} {s['weight']}%" for s in row["sources"])
        flow.append(
            Paragraph(f"<font color='#475569' size='7.5'>Combined from: {escape(made_of)}</font>", style["cell"])
        )
    if official_notice(row):
        flow.append(Paragraph(f"<font color='#475569' size='7.5'>{escape(official_notice(row))}</font>", style["cell"]))
        flow.append(Spacer(1, 4))
    if words:
        flow.append(Paragraph("<b>Comments</b>", style["normal"]))
        flow.extend(words)
        flow.append(Spacer(1, 6))
    if forecast_table is not None:
        flow.append(
            Paragraph(
                "<b>The school's grade estimates</b> <font color='#475569' size='7.5'>"
                "The school's own judgement, not a result awarded by an examination board.</font>",
                style["normal"],
            )
        )
        flow.append(forecast_table)
        flow.append(Spacer(1, 6))
    if exam.status != "published":
        flow.append(
            Paragraph("<font color='#b91c1c'><b>DRAFT</b> — these results are not published.</font>", style["normal"])
        )
    if snapshot:
        where = escape(verify_url) if verify_url else "the school's report verification page"
        code = escape(row.get("fingerprint") or "")
        note = Paragraph(
            f"<font color='#475569' size='7.5'>Version {snapshot.version}"
            + (f" · card fingerprint <b>{code}</b>" if code else "")
            + f"<br/>Scan the code, or open {where}, to confirm this card is genuine and current.</font>",
            style["cell"],
        )
        block = [[note, qr_drawing(verify_url, 22)]] if verify_url else [[note, ""]]
        verification = Table(block, colWidths=[150 * mm, 28 * mm])
        verification.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        flow.append(verification)
    flow.append(Spacer(1, 14))
    flow.append(signatures)
    return flow


def report_card_pdf(school, exam, enrollment, row, snapshot, verify_url=""):
    style = styles()
    return document(
        school,
        f"Report card · {exam.name}",
        report_card_flowables(school, exam, enrollment, row, snapshot, style, verify_url),
        subtitle=f"{row['student']} · {row['section']}",
        filename=f"report-card-{row.get('student_code') or enrollment.student.student_id}.pdf",
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
        flow.extend(letterhead(school, f"Report card · {exam.name}", f"{row['student']} · {row['section']}", style))
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


def admit_cards_pdf(school, exam, enrollments, schedules, papers_for_student=None):
    """
    One admit card per student, each listing the papers that student sits.

    `papers_for_student` maps an enrollment to its own papers. Without it every student gets
    every paper, which is right only for a class where everyone takes the same subjects.
    """
    style = styles()

    def rows_for(papers):
        return [
            [
                s.subject.name,
                s.date.strftime("%d %b %Y") if s.date else "To be announced",
                f"{s.start_time:%H:%M}" if s.start_time else "",
                s.room or "",
            ]
            for s in papers
        ]

    flow = []
    for index, enrollment in enumerate(enrollments):
        paper_rows = rows_for(papers_for_student(enrollment) if papers_for_student else schedules)
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
                ]
                + (
                    [("Result code", student.result_code)]
                    if school.public_results_enabled and student.result_code
                    else []
                ),
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
                "headline": headline(row),
                "rank": row["rank"] if shows_rank(row) else None,
            }
        )
    return rows
