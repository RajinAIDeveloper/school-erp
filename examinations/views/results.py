"""
Results: class sheets and reports, report cards, verification, progress over time, and the public lookup for families.
"""

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

from academics.models import ClassLevel, Section
from core.access import is_manager, may_use_section, require_permission, students_for, taught_sections
from core.exports import spreadsheet
from core.forms import TailwindFormMixin
from core.pdf import table_document
from students.models import Enrollment

from ..documents import card_rows
from ..exports import about, filter_rows
from ..grading import headline
from ..models import Exam, ResultSnapshot
from ..rulebooks import official_notice
from ..services import (
    build_result_sheet,
)
from .common import _cell_text, card_enrollment, mask_name, verification_url


def _class_report(request, sheet, kind, filters, show_rank, margin=5):
    """
    One of the class reports as a title, headers and rows, or None with a message saying why
    it does not apply to this class.
    """
    from ..exports import failed_subjects, grade_distribution, merit_list, merit_table, near_pass_mark, tabulation

    book, rows = sheet["rulebook"], sheet["rows"]
    preamble = about(sheet, filters)
    if kind in ("fails", "nearfail") and not book.pass_marks:
        messages.info(request, f"{book.label} has no pass marks, so there is no list of fails or near fails.")
        return None
    if kind == "fails":
        headers, body, counts = failed_subjects(rows)
        spread = ", ".join(f"{counts[n]} in {n} subject{'s' if n > 1 else ''}" for n in sorted(counts))
        note = f"{len(body)} of {len(rows)} student(s) failed at least one subject" + (f" ({spread})" if spread else "")
        return {
            "title": "Failed subjects",
            "headers": headers,
            "rows": body,
            "about": preamble,
            "note": note + ". A subject still without a mark is not counted.",
            "extra": [],
        }
    if kind == "nearfail":
        headers, body = near_pass_mark(rows, margin)
        return {
            "title": "Near the pass mark",
            "headers": headers,
            "rows": body,
            "about": [*preamble, ("Within", f"{margin} marks of the pass mark, above or below")],
            "note": f"{len(body)} subject score(s) within {margin} marks of the pass mark.",
            "extra": [],
        }
    if kind == "distribution":
        letters, lines = grade_distribution(sheet, rows)
        headers = ["Subject", "Sat", "Absent", "No mark yet", *letters]
        body = [
            [ln["subject"], ln["sat"], ln["absent"], ln["missing"], *[ln["counts"].get(x, "") for x in letters]]
            for ln in lines
        ]
        cumulative = [[ln["subject"], ln["sat"], *[ln["at_or_above"].get(x, "") for x in letters]] for ln in lines]
        return {
            "title": "Grade distribution",
            "headers": headers,
            "rows": body,
            "about": preamble,
            "note": "Counts of students who sat each subject. Percentages in the download are of those who sat it.",
            "extra": [("At or above (%)", ["Subject", "Sat", *[f"{x} or above" for x in letters]], cumulative)],
        }
    if kind == "tabulation":
        if not sheet["board"]:
            messages.info(request, "The tabulation sheet is for classes following the national curriculum.")
            return None
        headers, body = tabulation(rows)
        return {
            "title": "Tabulation sheet",
            "headers": headers,
            "rows": body,
            "about": preamble,
            "note": "",
            "extra": [],
        }
    if kind == "merit":
        if not show_rank:
            messages.info(
                request,
                f"Positions are off for this exam under {book.label}. Turn them on in the exam's settings "
                "to print a merit list.",
            )
            return None
        ranked, unranked = merit_list(rows)
        headers, body = merit_table(book, ranked, unranked)
        note = f"{len(ranked)} student(s) ranked among those shown"
        if unranked:
            note += f"; {len(unranked)} without a complete result are listed after, not ranked"
        return {
            "title": "Merit list",
            "headers": headers,
            "rows": body,
            "about": preamble,
            "note": note + ".",
            "extra": [],
        }
    return None


def _report_download(request, sheet, report, fmt):
    slug = report["title"].lower().replace(" ", "-")
    if fmt == "pdf":
        subtitle = " · ".join(value for _label, value in report["about"][1:4])
        if report["note"]:
            subtitle += " · " + report["note"]
        from django.utils.translation import gettext

        return table_document(
            request.school,
            f"{gettext(report['title'])} - {sheet['exam'].name}",
            report["headers"],
            report["rows"],
            subtitle=subtitle,
            filename=f"{slug}.pdf",
        )
    return spreadsheet(
        slug, report["headers"], report["rows"], fmt, extra_sheets=report["extra"], preamble=report["about"]
    )


class ResultsFilter(TailwindFormMixin, forms.Form):
    term = forms.ModelChoiceField(
        queryset=None, required=False, help_text="Narrows the exam list to one term of the year."
    )
    exam = forms.ModelChoiceField(queryset=None)
    class_level = forms.ModelChoiceField(queryset=None)
    section = forms.ModelChoiceField(queryset=None, required=False, help_text="Leave blank for a class-wide sheet.")
    report = forms.ChoiceField(choices=(), required=False)
    group = forms.ChoiceField(choices=(), required=False)
    shift = forms.ChoiceField(choices=(), required=False)
    version = forms.ChoiceField(choices=(), required=False)
    margin = forms.IntegerField(
        min_value=1,
        max_value=50,
        required=False,
        label="Near the pass mark: within",
        help_text="Marks either side of the pass mark, for the near-the-pass-mark list. 5 if left blank.",
    )

    def __init__(self, *args, user, school, **kwargs):
        super().__init__(*args, **kwargs)
        from academics.models import Group, Section, Term

        from ..exports import REPORTS

        self.fields["report"].choices = REPORTS
        # Stored results carry the labels, so the labels are what is matched.
        self.fields["group"].choices = [("", "All groups")] + [(label, label) for _v, label in Group.choices]
        self.fields["shift"].choices = [("", "All shifts")] + [
            (label, label) for _v, label in Section._meta.get_field("shift").choices
        ]
        self.fields["version"].choices = [("", "All versions")] + [
            (label, label) for _v, label in Section._meta.get_field("version").choices
        ]
        self.user, self.school = user, school
        self.fields["term"].queryset = Term.objects.filter(school=school).select_related("academic_year")
        self.fields["exam"].queryset = Exam.objects.filter(school=school)
        # A school running three terms a year accumulates exams quickly; picking a term
        # shortens the exam list to the ones that belong to it.
        raw_term = (self.data.get("term") or "").strip()
        if raw_term.isdigit():
            self.fields["exam"].queryset = self.fields["exam"].queryset.filter(term_id=int(raw_term))
        self.fields["class_level"].queryset = ClassLevel.objects.filter(school=school)
        # Any section this person has taught may be picked; whether they may see it for
        # the chosen exam is decided below, against that exam's own year.
        self.fields["section"].queryset = taught_sections(user, school)
        if not is_manager(user):
            self.fields["section"].required = True

    def clean(self):
        d = super().clean()
        if d.get("section") and d.get("class_level") and d["section"].class_level_id != d["class_level"].pk:
            raise forms.ValidationError("Section does not belong to this class.")
        exam, section, term = d.get("exam"), d.get("section"), d.get("term")
        if exam and term and exam.term_id != term.pk:
            self.add_error("exam", f"{exam.name} does not belong to {term}.")
        if exam and section and not may_use_section(self.user, self.school, section, exam.academic_year):
            raise forms.ValidationError(
                f"You did not teach {section} in {exam.academic_year}, so these results are not yours to read."
            )
        return d


@require_permission("examinations.view_mark", also="own sections only")
def results(request):
    form = ResultsFilter(request.GET or None, user=request.user, school=request.school)
    sheet, report = None, None
    if form.is_bound and form.is_valid():
        data = form.cleaned_data
        sheet = build_result_sheet(data["exam"], data["class_level"], data["section"])
        fmt = request.GET.get("format")
        book = sheet["rulebook"]
        show_rank = book.show_rank(sheet["exam"])
        picked = {k: data[k] for k in ("group", "shift", "version") if data.get(k)}
        if picked:
            from ..services import analyse, sheet_columns

            sheet["rows"] = filter_rows(sheet["rows"], **picked)
            sheet["summary"], sheet["subject_stats"] = analyse(sheet["rows"])
            sheet["columns"] = sheet_columns(sheet["rows"])
        kind = data.get("report") or "sheet"
        if kind != "sheet":
            report = _class_report(
                request, sheet, kind, ", ".join(picked.values()), show_rank, margin=data.get("margin") or 5
            )
            if report and fmt in ("csv", "xlsx", "pdf"):
                return _report_download(request, sheet, report, fmt)
        elif fmt in ("csv", "xlsx", "pdf"):
            columns = sheet["columns"]
            subjects = [name for _pk, name, _code in columns]
            # Only the columns the rulebook defines: no GPA or positions invented for a
            # programme that has neither.
            headers = (
                (["Class rank", "Section rank"] if show_rank else [])
                + ["Section", "Roll", "Student", *subjects, "Total", "Percent"]
                + (["GPA"] if book.has_gpa else [])
                + ["Overall"]
            )
            rows = [
                ([r["grade_rank"], r["rank"]] if show_rank else [])
                + [r["section"], r["roll"], r["student"], *[_cell_text(r, pk) for pk, _name, _code in columns]]
                + [r["total"], r["percent"]]
                + ([r["gpa"]] if book.has_gpa else [])
                + [headline(r)]
                for r in sheet["rows"]
            ]
            preamble = about(sheet, ", ".join(picked.values()))
            if fmt == "pdf":
                from ..documents import mark_text

                # On paper, marks as people write them (92, not 92.00); the spreadsheets keep the figures.
                first, last = (2 if show_rank else 0) + 3, (2 if show_rank else 0) + 3 + len(subjects)
                printed = [[mark_text(v) if first <= i <= last else v for i, v in enumerate(r)] for r in rows]
                subtitle = " · ".join(value for _label, value in preamble[1:4])
                return table_document(
                    request.school,
                    f"Result sheet - {sheet['exam'].name}",
                    headers,
                    printed,
                    subtitle=subtitle,
                    filename="result-sheet.pdf",
                )
            stats = sheet["subject_stats"]
            # Failed and pass percent only mean something where papers have pass marks.
            stat_keys = ["subject", "entered", "absent", "missing", "average", "highest", "lowest"]
            stat_heads = ["Subject", "Entered", "Absent", "Missing", "Average", "Highest", "Lowest"]
            if book.pass_marks:
                stat_keys += ["failed", "pass_pct"]
                stat_heads += ["Failed", "Pass percent"]
            return spreadsheet(
                "results",
                headers,
                rows,
                fmt,
                extra_sheets=[
                    (
                        "Subject analysis",
                        stat_heads,
                        [[s[k] for k in stat_keys] for s in stats],
                    )
                ],
                preamble=preamble,
            )
    return render(
        request,
        "examinations/results.html",
        {
            "form": form,
            "sheet": sheet,
            "report": report,
            "about": about(sheet) if sheet else [],
            "show_rank": sheet["rulebook"].show_rank(sheet["exam"]) if sheet else False,
            "page_title": "Results and subject analysis",
        },
    )


@require_permission(None, also="own children, or sections taught in the exam's year")
def report_card(request, exam_pk, student_pk):
    exam = get_object_or_404(Exam, pk=exam_pk, school=request.school)
    student, enr = card_enrollment(request, exam, student_pk)
    if not request.user.has_perm("examinations.view_mark") and exam.status != "published":
        raise PermissionDenied
    # The whole class, so the card can print the class's highest mark in each subject.
    sheet = build_result_sheet(exam, enr.class_level)
    row = next((r for r in sheet["rows"] if r["enrollment_id"] == enr.pk), None)
    if row is None:
        from django.http import Http404

        raise Http404
    from ..documents import class_highest, with_class_highest

    row = with_class_highest(row, class_highest(sheet["rows"]))
    snap = ResultSnapshot.objects.filter(exam=exam, enrollment=enr, version=exam.publication_version).first()
    if request.GET.get("format") == "pdf":
        from ..documents import report_card_pdf

        return report_card_pdf(request.school, exam, enr, row, snap, verify_url=verification_url(request, snap))
    from core.qr import qr_svg

    from ..documents import attendance_for, card_rows, shows_points

    lines = card_rows(row)
    link = verification_url(request, snap)
    attendance = row["attendance"] if "attendance" in row else attendance_for(enr, exam.end_date)
    from datetime import date as _date

    return render(
        request,
        "examinations/report_card.html",
        {
            "row": row,
            "exam": exam,
            "snapshot": snap,
            "lines": lines,
            "show_parts": any(line["parts"] for line in lines),
            "show_points": shows_points(row),
            "show_highest": any(line["highest"] for line in lines),
            "attendance": attendance,
            "attendance_until": _date.fromisoformat(attendance["until"])
            if attendance and attendance.get("until")
            else None,
            "show_effort": any(line["effort"] for line in lines),
            "official_notice": official_notice(row),
            "comment_span": 4
            + shows_points(row)
            + any(line["highest"] for line in lines)
            + any(line["parts"] for line in lines)
            + any(line["effort"] for line in lines),
            "effort_label": row.get("effort_label") or "Effort",
            "board": row.get("system") == "national",
            # The working is for staff checking a result, never for a family's view of it.
            "trace": row.get("trace") if request.user.has_perm("examinations.view_mark") else None,
            "verify_url": link,
            "qr": qr_svg(link) if link else "",
            "page_title": "Report card",
        },
    )


def verify(request, code):
    """
    Public check that a report card is genuine.

    What it proves, and no more: that this school published this result for this student,
    in this version, and whether a newer version has replaced it. It shows what someone
    holding the card needs to compare against the paper — the student's initials, class,
    roll, GPA and result, and the card fingerprint — so an altered grade or name no longer
    matches. It does not show subject marks, the full name or anything else about the child.

    The code is a random 122-bit identifier printed only on the card itself, so reaching this
    page means holding the card: nobody can walk through it by guessing.
    """
    snapshot = get_object_or_404(
        ResultSnapshot.objects.select_related("exam__academic_year", "school"), verification_code=code
    )
    current = snapshot.exam.status == "published" and snapshot.version == snapshot.exam.publication_version
    payload = snapshot.payload or {}
    return render(
        request,
        "examinations/verify.html",
        {
            "snapshot": snapshot,
            "school": snapshot.school,
            "exam": snapshot.exam,
            "current": current,
            "initials": mask_name(payload.get("student", "")),
            "class_name": payload.get("section", ""),
            "roll": payload.get("roll", ""),
            "gpa": payload.get("gpa"),
            "result": payload.get("result", ""),
            "headline": headline(payload),
            "fingerprint": payload.get("fingerprint", ""),
            "page_title": "Report verification",
        },
    )


@require_permission("examinations.view_mark", also="own sections, published only")
def report_cards(request):
    """Every card for a section in one PDF, for printing in a single run."""
    from ..documents import bulk_report_cards_pdf

    exam = get_object_or_404(Exam, school=request.school, pk=request.GET.get("exam", 0))
    section = get_object_or_404(Section, school=request.school, pk=request.GET.get("section", 0))
    if not may_use_section(request.user, request.school, section, exam.academic_year):
        raise PermissionDenied("You can print cards only for sections you taught that year.")
    if exam.status != "published" and not is_manager(request.user):
        # A draft card is a working document. Thirty of them printed and sent home before
        # the results are agreed cannot be recalled.
        raise PermissionDenied(
            "These results are not published yet. A whole section can be printed once they are, "
            "or a head can print the drafts."
        )
    from ..documents import class_highest, with_class_highest

    # The whole class, for its highest marks; only this section's cards are printed.
    sheet = build_result_sheet(exam, section.class_level)
    highest = class_highest(sheet["rows"])
    enrollments = {
        e.pk: e
        for e in Enrollment.objects.filter(
            school=request.school, section=section, academic_year=exam.academic_year
        ).select_related("student", "section__class_level")
    }
    snapshots = {s.enrollment_id: s for s in ResultSnapshot.objects.filter(exam=exam, version=exam.publication_version)}
    cards = [
        (
            enrollments[row["enrollment_id"]],
            with_class_highest(row, highest),
            snapshots.get(row["enrollment_id"]),
            verification_url(request, snapshots.get(row["enrollment_id"])),
        )
        for row in sheet["rows"]
        if row["enrollment_id"] in enrollments
    ]
    return bulk_report_cards_pdf(request.school, exam, cards)


@require_permission(None)
def progress(request, student_pk):
    """One student's published results across the exams they have sat."""
    from ..documents import progress_rows

    student = get_object_or_404(students_for(request.user, request.school), pk=student_pk)
    enrollments = {
        e.academic_year_id: e
        for e in Enrollment.objects.filter(school=request.school, student=student).select_related(
            "section__class_level", "academic_year"
        )
    }
    exams = Exam.objects.filter(school=request.school, academic_year_id__in=enrollments, status="published").order_by(
        "academic_year__start_date", "start_date", "name"
    )
    rows = progress_rows(exams, enrollments)
    headers = ["Examination", "Class", "Total", "Percent", "Result", "Rank"]
    table = [[r["exam"].name, r["section"], r["total"], r["percent"], r["headline"], r["rank"]] for r in rows]
    if request.GET.get("format") == "pdf":
        return table_document(
            request.school,
            "Progress report",
            headers,
            table,
            subtitle=f"{student.full_name} ({student.student_id})",
            filename=f"progress-{student.student_id}.pdf",
            align_right=(2, 3, 4, 5),
        )
    return render(
        request,
        "examinations/progress.html",
        {"student": student, "rows": rows, "page_title": f"Progress: {student.full_name}"},
    )


def public_results(request, slug):
    """
    Families look up a published result without signing in, when the school allows it.

    The student ID and the result code from the admit card must both match. A wrong pair
    says only that nothing matched, and too many from one address are turned away.
    """
    from core.models import School

    from ..public import lookup, published_results, throttled

    school = get_object_or_404(School, slug=slug, is_active=True)
    if not school.public_results_enabled:
        from django.http import Http404

        raise Http404
    from core.security import client_ip

    address = client_ip(request)
    context = {"school": school, "student": None, "error": "", "page_title": "Results"}
    if request.method == "POST":
        if throttled(address, school, request.POST.get("student_id", "")):
            context["error"] = "Too many attempts. Try again in 15 minutes."
        else:
            student = lookup(school, request.POST.get("student_id", ""), request.POST.get("code", ""), address)
            if student is None:
                context["error"] = "No result matches that student ID and result code. Check both and try again."
            else:
                exams, combined = published_results(student)
                context.update(
                    {
                        "student": student,
                        "results": [
                            {
                                "title": f"{s.exam.name} ({s.exam.academic_year})",
                                "row": s.payload,
                                "lines": card_rows(s.payload),
                                "notice": official_notice(s.payload),
                            }
                            for s in exams
                        ]
                        + [
                            {
                                "title": f"{s.combined.name} ({s.combined.academic_year})",
                                "row": s.payload,
                                "lines": card_rows(s.payload),
                                "notice": official_notice(s.payload),
                            }
                            for s in combined
                        ],
                    }
                )
    response = render(request, "examinations/public_results.html", context)
    response["Cache-Control"] = "no-store"
    return response
