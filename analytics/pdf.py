"""The progress report: a student's progress on paper, in the reader's language."""

from xml.sax.saxutils import escape

from django.utils.translation import gettext
from reportlab.graphics.charts.barcharts import HorizontalBarChart
from reportlab.graphics.shapes import Drawing, String
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer

from core.pdf import bangla_font, data_table, document, styles
from examinations.parts import plain


def _value(value, suffix=""):
    return "—" if value is None else f"{plain(value)}{suffix}"


def _subject_chart(rows, style):
    """Horizontal bars: the student's percentage and the class average per subject."""
    if not rows:
        return None
    height = 22 * len(rows) + 40
    drawing = Drawing(178 * mm, height)
    chart = HorizontalBarChart()
    chart.x, chart.y = 120, 20
    chart.width, chart.height = 178 * mm - 150, height - 30
    chart.data = [
        [float(row["percent"] or 0) for row in reversed(rows)],
        [float(row["average"] or 0) for row in reversed(rows)],
    ]
    chart.categoryAxis.categoryNames = [row["subject"][:28] for row in reversed(rows)]
    font = bangla_font() or "Helvetica"
    chart.categoryAxis.labels.fontName = font
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.valueMin, chart.valueAxis.valueMax, chart.valueAxis.valueStep = 0, 100, 25
    chart.valueAxis.labels.fontSize = 7
    chart.bars[0].fillColor = colors.HexColor("#4f46e5")
    chart.bars[1].fillColor = colors.HexColor("#cbd5e1")
    chart.barSpacing, chart.groupSpacing = 1, 6
    drawing.add(chart)
    drawing.add(String(120, 6, gettext("Student · Class average"), fontName=font, fontSize=7, fillColor=colors.grey))
    return drawing


def progress_pdf(school, data):
    style = styles()
    student = data["student"]
    latest = data["latest"]
    flow = [
        Paragraph(
            f"<b>{escape(student.full_name)}</b> · {escape(student.student_id)} · {escape(str(latest['class']))}",
            style["normal"],
        ),
        Spacer(1, 6),
    ]
    if data["overall_shown"]:
        flow.append(
            data_table(
                [
                    gettext("Exam"),
                    gettext("Percent"),
                    gettext("GPA"),
                    gettext("Result"),
                    gettext("Class average"),
                    gettext("Class highest"),
                    gettext("Position"),
                ],
                [
                    [
                        row["exam"].name,
                        _value(row["percent"], "%"),
                        _value(row["gpa"]),
                        gettext(row["result"]) if row["result"] else "—",
                        _value(row["average"], "%"),
                        _value(row["highest"], "%"),
                        row["rank"] or "—",
                    ]
                    for row in data["exams"]
                ],
                style,
                align_right=(1, 2, 4, 5, 6),
            )
        )
        flow.append(Spacer(1, 8))
    flow.append(Paragraph(f"<b>{escape(gettext('Latest exam'))}: {escape(latest['exam'].name)}</b>", style["normal"]))
    flow.append(
        data_table(
            [
                gettext("Subject"),
                gettext("Marks"),
                gettext("Percent"),
                gettext("Grade"),
                gettext("Class average"),
                gettext("Class highest"),
            ],
            [
                [
                    row["subject"],
                    "ABS" if row["absent"] else f"{_value(row['score'])} / {_value(row['full'])}",
                    _value(row["percent"], "%"),
                    row["letter"] or "—",
                    _value(row["average"], "%"),
                    _value(row["highest"], "%"),
                ]
                for row in data["latest_subjects"]
            ],
            style,
            align_right=(1, 2, 4, 5),
        )
    )
    chart = _subject_chart(data["latest_subjects"], style)
    if chart is not None:
        flow += [Spacer(1, 6), chart]
    if data["strengths"] or data["attention"]:
        flow.append(Spacer(1, 6))
        if data["strengths"]:
            flow.append(
                Paragraph(
                    f"<b>{escape(gettext('Going well'))}</b>: {escape(', '.join(data['strengths']))}", style["normal"]
                )
            )
        if data["attention"]:
            needs = ", ".join(f"{name} ({gettext(reason)})" for name, reason in data["attention"])
            flow.append(Paragraph(f"<b>{escape(gettext('Needs attention'))}</b>: {escape(needs)}", style["normal"]))
    if data["attendance"]:
        flow.append(Spacer(1, 8))
        flow.append(Paragraph(f"<b>{escape(gettext('Attendance'))}</b>", style["normal"]))
        flow.append(
            data_table(
                [gettext("Month"), gettext("Present"), gettext("Days"), gettext("Percent")],
                [
                    [f"{month:%b %Y}", present, total, f"{percent}%" if percent is not None else "—"]
                    for month, present, total, percent in data["attendance"]
                ],
                style,
                align_right=(1, 2, 3),
            )
        )
    flow.append(Spacer(1, 8))
    flow.append(
        Paragraph(
            f"<font color='#475569' size='7.5'>{escape(gettext('From the published results only. Class figures are for the whole class in each exam; no other student is named.'))}</font>",
            style["cell"],
        )
    )
    return document(
        school,
        gettext("Progress report"),
        flow,
        subtitle=student.full_name,
        filename=f"progress-{student.student_id}.pdf",
    )
