"""
The printed transcript, drawn from the frozen record every time it is downloaded.

Every page carries the serial, the fingerprint and "page n of N", so a page cannot be taken from
one transcript and slipped into another. The last page carries the QR code that opens the
public check, which shows the same grades to compare.
"""

from xml.sax.saxutils import escape

from reportlab.lib.units import mm
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

from core.pdf import data_table, document, styles
from core.qr import qr_drawing


def _date(iso):
    from datetime import date

    return f"{date.fromisoformat(iso):%d %b %Y}" if iso else ""


def _grade_point(value):
    if value in (None, ""):
        return ""
    from examinations.parts import plain

    try:
        return plain(value)
    except Exception:  # noqa: BLE001 - an odd stored value prints as it is
        return str(value)


def _facts(pairs, style):
    cells = [
        Paragraph(f"<font color='#475569'>{escape(label)}</font><br/><b>{escape(str(value))}</b>", style["cell"])
        for label, value in pairs
        if value
    ]
    rows = [cells[i : i + 3] + [""] * (3 - len(cells[i : i + 3])) for i in range(0, len(cells), 3)]
    table = Table(rows, colWidths=[59 * mm] * 3)
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
    return table


def transcript_pdf(transcript, verify_url):
    data = transcript.payload
    style = styles()
    student = data["student"]
    flow = [
        _facts(
            [
                ("Student", student["name"] + (f" ({student['name_bn']})" if student.get("name_bn") else "")),
                ("Student ID", student["student_id"]),
                ("Date of birth", _date(student["date_of_birth"])),
                ("Admitted", _date(student["admission_date"])),
                ("Left", _date(student["left_on"]))
                if student.get("left_on")
                else ("Status", student.get("status", "")),
                ("Issued", _date(data["issued_on"])),
            ],
            style,
        ),
        Spacer(1, 6),
    ]
    with_percent = any(s.get("percent") is not None for year in data["years"] for s in year["subjects"])
    for year in data["years"]:
        heading = " · ".join(
            part
            for part in [
                year["year"],
                year["class"],
                f"Section {year['section']}" if year["section"] else "",
                f"{year['group']} group" if year["group"] else "",
            ]
            if part
        )
        basis = year["basis"]
        if year.get("sources"):
            basis += (
                " (combined from "
                + ", ".join(
                    f"{s['exam']} {s['weight']}%" if s.get("weight") is not None else s["exam"] for s in year["sources"]
                )
                + ")"
            )
        if year.get("in_progress"):
            basis += " · year in progress"
        headers = ["Subject", "Code", "Grade", "Grade point"] + (["Percent"] if with_percent else [])
        rows = [
            [
                subject["name"]
                + (" (4th subject)" if subject.get("fourth") else "")
                + (f" {subject['level']}" if subject.get("level") else ""),
                subject.get("code", ""),
                subject.get("letter", ""),
                _grade_point(subject.get("grade_point")),
            ]
            + ([_grade_point(subject.get("percent"))] if with_percent else [])
            for subject in year["subjects"]
        ]
        summary = [
            part
            for part in [
                f"GPA {_grade_point(year['gpa'])}" if year.get("gpa") not in (None, "") else "",
                year.get("result", "") if year.get("result") and year.get("gpa") not in (None, "") else "",
                year.get("headline", "") if year.get("gpa") in (None, "") else "",
                f"Attendance {year['attendance']}" if year.get("attendance") else "",
            ]
            if part
        ]
        block = [
            Paragraph(f"<b>{escape(heading)}</b>", style["normal"]),
            Paragraph(escape(basis) + (f" · {escape(year['system'])}" if year.get("system") else ""), style["cell"]),
            Spacer(1, 3),
            data_table(headers, rows, style),
            Paragraph(escape(" · ".join(summary)), style["cell"]),
            Spacer(1, 8),
        ]
        flow.append(KeepTogether(block))
    if data.get("board"):
        flow.append(Paragraph("<b>Official results from awarding bodies</b>", style["normal"]))
        for group in data["board"]:
            flow.append(Paragraph(escape(f"{group['body']} · {group['series']}"), style["cell"]))
            flow.append(
                data_table(
                    ["Qualification", "Code", "Subject", "Grade", "Points"],
                    [
                        [
                            r["qualification"],
                            r["code"],
                            r["title"] + (f" ({r['level']})" if r.get("level") else ""),
                            r["grade"],
                            r.get("points", ""),
                        ]
                        for r in group["results"]
                    ],
                    style,
                )
            )
            flow.append(Spacer(1, 6))
    if data.get("predicted"):
        flow.append(
            KeepTogether(
                [
                    Paragraph("<b>Predicted grades</b>", style["normal"]),
                    data_table(
                        ["Subject", "Predicted grade", "As of"],
                        [[p["subject"], p["grade"], _date(p["as_of"])] for p in data["predicted"]],
                        style,
                    ),
                    Paragraph("The school's judgement of the likely grade. Not a result.", style["cell"]),
                    Spacer(1, 8),
                ]
            )
        )
    if data.get("keys"):
        for number, key in enumerate(data["keys"]):
            # The heading travels with the first key, never left alone at the foot of a page.
            heading = [Paragraph("<b>Grading keys</b>", style["normal"])] if number == 0 else []
            flow.append(
                KeepTogether(
                    heading
                    + [
                        Paragraph(escape(" · ".join(p for p in [key["label"], key["scale"]] if p)), style["cell"]),
                        data_table(
                            ["Grade", "From %", "To %", "Grade point"],
                            [
                                [r["letter"], _grade_point(r["min"]), _grade_point(r["max"]), _grade_point(r["point"])]
                                for r in key["rules"]
                            ],
                            style,
                        ),
                        Spacer(1, 5),
                    ]
                )
            )
    for note in data.get("notes", []):
        flow.append(Paragraph(escape(note), style["cell"]))
    flow.append(Spacer(1, 10))
    check = Table(
        [
            [
                qr_drawing(verify_url, size_mm=24),
                Paragraph(
                    f"Check this transcript at<br/><font size='7'>{escape(verify_url)}</font><br/>"
                    f"Serial <b>{escape(transcript.serial)}</b> · fingerprint <b>{escape(transcript.fingerprint)}</b>",
                    style["cell"],
                ),
            ]
        ],
        colWidths=[28 * mm, 150 * mm],
    )
    check.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    # Two signature lines with a gap between them, and a space for the seal.
    signatures = Table(
        [["", "", "", "", ""], ["Principal", "", "Examination officer", "", "School seal"]],
        colWidths=[52 * mm, 11 * mm, 52 * mm, 11 * mm, 52 * mm],
        rowHeights=[16 * mm, 6 * mm],
    )
    signatures.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 1), (0, 1), 0.5, "#0f172a"),
                ("LINEABOVE", (2, 1), (2, 1), 0.5, "#0f172a"),
                ("FONTSIZE", (0, 1), (-1, 1), 8),
                ("ALIGN", (0, 1), (-1, 1), "CENTER"),
            ]
        )
    )
    flow.append(KeepTogether([check, Spacer(1, 6), signatures]))
    school_name = data["school"]["name"]

    def footer(page, pages):
        return f"{school_name} · Transcript {transcript.serial} · Fingerprint {transcript.fingerprint} · Page {page} of {pages}"

    return document(
        transcript.school,
        "Academic Transcript",
        flow,
        subtitle=f"Serial {transcript.serial}",
        filename=f"transcript-{transcript.serial}.pdf",
        footer=footer,
    )
