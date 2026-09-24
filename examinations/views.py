from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from academics.models import ClassLevel, Section
from core.access import is_manager, may_use_section, require_permission, sections_for, students_for, taught_sections
from core.exports import spreadsheet
from core.forms import TailwindFormMixin
from core.generic import ERPListView
from core.pdf import table_document
from students.models import Enrollment

from .documents import card_rows
from .exports import about, filter_rows
from .grading import headline
from .models import Exam, ExamSchedule, GradeRule, GradeScale, Mark, ResultSnapshot, UnlockRequest
from .rulebooks import official_notice
from .services import (
    MarkEntryError,
    active_unlock,
    assert_can_mark,
    build_result_sheet,
    expected_marks,
    publish_exam,
    review_unlock,
    save_mark,
    save_marks,
)
from .subjects import enrollments_taking, subject_plan


def verification_url(request, snapshot):
    """
    The address printed on a report card, in full.

    A code on its own is no use to the employer or college holding the card: they have to
    know where to type it. This is the whole link, so it can be read off paper.
    """
    if snapshot is None:
        return ""
    return request.build_absolute_uri(reverse("examinations:verify", args=[snapshot.verification_code]))


def _cell_text(row, schedule_id):
    """One paper's mark for a spreadsheet cell: the score, ABS, blank if not sat."""
    cell = next((c for c in row["cells"] if c["schedule_id"] == schedule_id), None)
    if cell is None:
        return ""
    if cell.get("exempt"):
        return "EX"
    if cell["absent"]:
        return "ABS"
    return cell["score"] if not cell["missing"] else ""


class ExamListView(ERPListView):
    model = Exam
    permission_required = "examinations.view_exam"
    page_title = "Exams and results"
    columns = (("Exam", "name"), ("Year", "academic_year"), ("Start", "start_date"), ("Status", "status", "badge"))
    create_url_name = "examinations:exam_create"
    update_url_name = "examinations:exam_update"
    detail_url_name = "examinations:exam_detail"
    extra_actions = (
        ("Schedules", "examinations:schedule_list", "examinations.view_examschedule"),
        ("Mark entry", "examinations:marks", "examinations.view_mark"),
        ("Results", "examinations:results", "examinations.view_mark"),
        ("Grading", "examinations:scale_list", "examinations.view_gradescale"),
        ("Unlock requests", "examinations:unlocks", "examinations.view_mark"),
    )


@require_permission("examinations.view_exam")
def exam_detail(request, pk):
    exam = get_object_or_404(
        Exam.objects.select_related("academic_year", "term", "grade_scale"), school=request.school, pk=pk
    )
    schedules = exam.schedules.select_related("subject", "class_level").order_by("date", "class_level__order")
    sections = sections_for(request.user, request.school).select_related("class_level")
    marks_entered = Mark.objects.filter(schedule__exam=exam).count()
    # Each paper expects only the students who sit it: a Humanities student is not "missing"
    # a Physics mark.
    expected = sum(expected_marks(s) for s in schedules)
    return render(
        request,
        "examinations/exam.html",
        {
            "exam": exam,
            "schedules": schedules,
            "sections": sections,
            "marks_entered": marks_entered,
            "marks_expected": expected,
            "page_title": str(exam),
            "can_publish": is_manager(request.user) and request.user.has_perm("examinations.change_exam"),
            "checklist": _checklist_for(request.user, exam),
            "clashes": _clashes_for(exam),
        },
    )


def _clashes_for(exam):
    """Timetable clashes, while there is still time to move a paper."""
    if exam.status == "published":
        return []
    from .clashes import timetable_clashes

    return timetable_clashes(exam)


def _checklist_for(user, exam):
    """The pre-publication checks, for the people who can publish, while it is still a draft."""
    if exam.status == "published" or not (is_manager(user) and user.has_perm("examinations.change_exam")):
        return None
    from .checklist import publication_checklist

    return publication_checklist(exam)


@require_permission("examinations.change_exam")
@require_POST
def publish(request, pk):
    exam = get_object_or_404(Exam, school=request.school, pk=pk)
    try:
        publish_exam(exam, request.user)
        messages.success(request, "Results published and snapshotted. Marks are now locked.")
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    return redirect("examinations:exam_detail", pk=pk)


class MarkFilter(TailwindFormMixin, forms.Form):
    schedule = forms.ModelChoiceField(queryset=None)
    section = forms.ModelChoiceField(queryset=None)

    def __init__(self, *args, user, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["schedule"].queryset = ExamSchedule.objects.filter(school=school).select_related(
            "exam", "class_level", "subject"
        )
        self.fields["section"].queryset = sections_for(user, school)

    def clean(self):
        d = super().clean()
        if d.get("schedule") and d.get("section") and d["schedule"].class_level_id != d["section"].class_level_id:
            raise forms.ValidationError("Select a section belonging to this exam paper's class.")
        return d


class MarkForm(TailwindFormMixin, forms.Form):
    score = forms.DecimalField(max_digits=6, decimal_places=2, required=False)
    absent = forms.BooleanField(required=False)
    expected_version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


@require_permission("examinations.view_mark", also="assigned subjects and sections only")
def marks(request):
    """
    A grid: every student of the section on one screen, saved in one submission.

    Entering marks one student at a time was the single slowest thing a teacher had to do
    in this system.
    """
    selector = MarkFilter(request.GET or request.POST or None, user=request.user, school=request.school)
    rows, schedule, section, locked = [], None, None, False
    row_errors = {}
    # What the teacher typed, kept so a rejected submission is shown back as entered rather
    # than silently replaced by the stored marks.
    typed = {}

    components = []
    if selector.is_bound and selector.is_valid():
        schedule = selector.cleaned_data["schedule"]
        section = selector.cleaned_data["section"]
        # Authorised for this subject in this section, not merely somewhere in the class:
        # teaching Math in A and Science in B gives no sight of B's Math marks.
        assert_can_mark(request.user, schedule, section=section)
        locked = schedule.exam.status == "published" and not active_unlock(request.user, schedule)
        components = list(schedule.components.all())
        enrollments = enrollments_taking(
            schedule,
            list(
                Enrollment.objects.filter(
                    school=request.school, academic_year=schedule.exam.academic_year, section=section
                )
                .select_related("student")
                .prefetch_related("chosen_subjects")
                .order_by("roll_number")
            ),
            subject_plan(schedule.exam.academic_year, schedule.class_level),
        )
        existing = {
            mark.enrollment_id: mark for mark in Mark.objects.filter(schedule=schedule, enrollment__in=enrollments)
        }

        manager = is_manager(request.user)
        if request.method == "POST":
            if locked:
                raise PermissionDenied("Results are published. Request an unlock before editing.")
            if not request.user.has_perm("examinations.change_mark"):
                raise PermissionDenied
            submitted, invalid = [], {}
            for enrollment in enrollments:
                prefix = str(enrollment.pk)
                absent = request.POST.get(f"{prefix}-absent") == "on"
                stored = existing.get(enrollment.pk)
                # Only managers decide exemptions; everyone else's save keeps what is stored.
                exempt = request.POST.get(f"{prefix}-exempt") == "on" if manager else bool(stored and stored.is_exempt)
                if exempt:
                    absent = False
                typed[enrollment.pk] = {
                    "exempt": exempt,
                    "absent": absent,
                    "score": request.POST.get(f"{prefix}-score", "").strip(),
                    "parts": {c.code: request.POST.get(f"{prefix}-{c.code}", "").strip() for c in components},
                }
                try:
                    version = int(request.POST.get(f"{prefix}-version", "0"))
                except ValueError:
                    version = 0
                score, parts = None, {}
                if components:
                    for component in components:
                        raw_part = request.POST.get(f"{prefix}-{component.code}", "").strip()
                        if raw_part and not absent:
                            try:
                                parts[component.code] = Decimal(raw_part)
                            except InvalidOperation:
                                invalid[enrollment.pk] = f"{component.name}: '{raw_part}' is not a number."
                    if enrollment.pk in invalid:
                        continue
                    if exempt:
                        parts = {}
                else:
                    raw = request.POST.get(f"{prefix}-score", "").strip()
                    if raw and not absent and not exempt:
                        try:
                            score = Decimal(raw)
                        except InvalidOperation:
                            invalid[enrollment.pk] = f"'{raw}' is not a number."
                            continue
                submitted.append((enrollment, score, absent, version, parts, exempt))
            if invalid:
                row_errors = invalid
            else:
                try:
                    saved = save_marks(user=request.user, schedule=schedule, section=section, rows=submitted)
                    if saved:
                        messages.success(request, f"Saved {len(saved)} change(s).")
                    else:
                        messages.info(request, "Nothing had changed, so nothing was saved.")
                    return redirect(
                        reverse("examinations:marks")
                        + "?"
                        + urlencode({"schedule": schedule.pk, "section": section.pk})
                    )
                except MarkEntryError as exc:
                    row_errors = exc.errors
                    messages.error(request, "Nothing was saved. Fix the rows marked below and submit again.")
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))

        for enrollment in enrollments:
            mark = existing.get(enrollment.pk)
            stored_parts = (mark.component_marks or {}) if mark and not mark.is_absent else {}
            stored_score = mark.marks_obtained if mark and not mark.is_absent else None
            entry = typed.get(enrollment.pk)
            rows.append(
                {
                    "enrollment": enrollment,
                    "mark": mark,
                    # Stored values drive the progress summary; the inputs show what was typed.
                    "score": stored_score,
                    "absent": bool(mark and mark.is_absent),
                    "input_score": entry["score"] if entry else ("" if stored_score is None else stored_score),
                    "input_absent": entry["absent"] if entry else bool(mark and mark.is_absent),
                    "exempt": bool(mark and mark.is_exempt),
                    "input_exempt": entry["exempt"] if entry else bool(mark and mark.is_exempt),
                    "version": mark.version if mark else 0,
                    "parts": [
                        (component, entry["parts"][component.code] if entry else stored_parts.get(component.code, ""))
                        for component in components
                    ],
                    "error": row_errors.get(enrollment.pk),
                }
            )

    entered = sum(1 for row in rows if row["score"] is not None or row["absent"] or row["exempt"])
    scored = [row["score"] for row in rows if row["score"] is not None and not row["absent"]]
    # Averaged over the students who actually sat the paper, so an absence does not drag
    # the class down and a blank row does not count as a zero.
    average = (sum(scored) / len(scored)).quantize(Decimal("0.01")) if scored else None
    passed = sum(1 for score in scored if schedule and score >= schedule.pass_marks) if scored and schedule else 0
    return render(
        request,
        "examinations/marks.html",
        {
            "selector": selector,
            "rows": rows,
            "schedule": schedule,
            "section": section,
            "locked": locked,
            "entered": entered,
            "missing": len(rows) - entered,
            "average": average,
            "components": components,
            "may_exempt": is_manager(request.user),
            "average_percent": (
                (average * 100 / schedule.full_marks).quantize(Decimal("0.1"))
                if average is not None and schedule and schedule.full_marks
                else None
            ),
            "sat": len(scored),
            "passed": passed,
            "page_title": "Mark entry",
        },
    )


@require_permission("examinations.change_mark")
@require_POST
def mark_save(request):
    selector = MarkFilter(request.POST, user=request.user, school=request.school)
    form = MarkForm(request.POST)
    if selector.is_valid() and form.is_valid():
        schedule = selector.cleaned_data["schedule"]
        section = selector.cleaned_data["section"]
        try:
            enrollment_pk = int(request.POST.get("enrollment", ""))
        except (TypeError, ValueError):
            from django.http import HttpResponseBadRequest

            return HttpResponseBadRequest("Invalid enrollment.")
        enrollment = get_object_or_404(
            Enrollment,
            pk=enrollment_pk,
            school=request.school,
            academic_year=schedule.exam.academic_year,
            section=section,
        )
        try:
            save_mark(user=request.user, schedule=schedule, enrollment=enrollment, **form.cleaned_data)
            messages.success(request, f"Saved marks for {enrollment.student.full_name}.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
    else:
        messages.error(request, "Invalid mark: " + selector.errors.as_text() + " " + form.errors.as_text())
    from urllib.parse import urlencode

    from django.urls import reverse

    return redirect(
        reverse("examinations:marks")
        + "?"
        + urlencode({"schedule": request.POST.get("schedule", ""), "section": request.POST.get("section", "")})
    )


def _class_report(request, sheet, kind, filters, show_rank):
    """
    One of the class reports as a title, headers and rows, or None with a message saying why
    it does not apply to this class.
    """
    from .exports import grade_distribution, merit_list, merit_table, tabulation

    book, rows = sheet["rulebook"], sheet["rows"]
    preamble = about(sheet, filters)
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
        return table_document(
            request.school,
            f"{report['title']} - {sheet['exam'].name}",
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

    def __init__(self, *args, user, school, **kwargs):
        super().__init__(*args, **kwargs)
        from academics.models import Group, Section, Term

        from .exports import REPORTS

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
            from .services import analyse, sheet_columns

            sheet["rows"] = filter_rows(sheet["rows"], **picked)
            sheet["summary"], sheet["subject_stats"] = analyse(sheet["rows"])
            sheet["columns"] = sheet_columns(sheet["rows"])
        kind = data.get("report") or "sheet"
        if kind != "sheet":
            report = _class_report(request, sheet, kind, ", ".join(picked.values()), show_rank)
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
                from .documents import mark_text

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


def card_enrollment(request, exam, student_pk):
    """
    The enrollment a report card is for, if this person may see it.

    One rule for every historical card, the same one bulk printing uses: staff are judged by
    what they taught in the exam's own year, families by whether the child is theirs. A
    teacher can reprint last year's card for the section they taught last year, and cannot
    print this year's card for a pupil they merely teach now in another subject.
    """
    from students.models import Student

    student = get_object_or_404(Student, school=request.school, pk=student_pk)
    enr = get_object_or_404(Enrollment, student=student, academic_year=exam.academic_year)
    user = request.user
    if is_manager(user):
        allowed = True
    elif hasattr(user, "student_profile") or hasattr(user, "guardian_profile"):
        allowed = students_for(user, request.school).filter(pk=student.pk).exists()
    elif user.has_perm("examinations.view_mark"):
        allowed = may_use_section(user, request.school, enr.section, exam.academic_year)
    else:
        allowed = students_for(user, request.school).filter(pk=student.pk).exists()
    if not allowed:
        from django.http import Http404

        raise Http404
    return student, enr


@require_permission(None, also="own children, or sections taught in the exam's year")
def report_card(request, exam_pk, student_pk):
    exam = get_object_or_404(Exam, pk=exam_pk, school=request.school)
    student, enr = card_enrollment(request, exam, student_pk)
    if not request.user.has_perm("examinations.view_mark") and exam.status != "published":
        raise PermissionDenied
    sheet = build_result_sheet(exam, enr.class_level, enr.section)
    row = next((r for r in sheet["rows"] if r["enrollment_id"] == enr.pk), None)
    if row is None:
        from django.http import Http404

        raise Http404
    snap = ResultSnapshot.objects.filter(exam=exam, enrollment=enr, version=exam.publication_version).first()
    if request.GET.get("format") == "pdf":
        from .documents import report_card_pdf

        return report_card_pdf(request.school, exam, enr, row, snap, verify_url=verification_url(request, snap))
    from core.qr import qr_svg

    from .documents import attendance_for, card_rows, shows_points

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
            "attendance": attendance,
            "attendance_until": _date.fromisoformat(attendance["until"])
            if attendance and attendance.get("until")
            else None,
            "show_effort": any(line["effort"] for line in lines),
            "official_notice": official_notice(row),
            "comment_span": 4
            + shows_points(row)
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


def mask_name(name):
    """Initials and the length of each word: enough to match a card, not enough to read it off."""
    return " ".join(word[0] + "•" * (len(word) - 1) for word in str(name).split() if word)


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


@require_permission("examinations.view_mark", also="own requests unless a manager")
def unlocks(request):
    qs = UnlockRequest.objects.filter(school=request.school).select_related("schedule", "requested_by")
    if not is_manager(request.user):
        qs = qs.filter(requested_by=request.user)
    return render(
        request,
        "examinations/unlocks.html",
        {"rows": qs, "can_review": is_manager(request.user), "page_title": "Mark unlock requests"},
    )


@require_permission("examinations.change_mark")
@require_POST
def request_unlock(request, pk):
    schedule = get_object_or_404(ExamSchedule, school=request.school, pk=pk)
    assert_can_mark(request.user, schedule)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "A correction reason is required.")
    elif schedule.exam.status != "published":
        messages.error(request, "This exam is not locked.")
    else:
        UnlockRequest.objects.create(school=request.school, schedule=schedule, requested_by=request.user, reason=reason)
        messages.success(request, "Unlock requested for this subject and class.")
    return redirect("examinations:unlocks")


@require_permission("examinations.change_exam")
@require_POST
def unlock_review(request, pk):
    obj = get_object_or_404(UnlockRequest, school=request.school, pk=pk)
    try:
        review_unlock(obj, request.user, request.POST.get("decision") == "approve")
        messages.success(request, "Request reviewed.")
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    return redirect("examinations:unlocks")


@require_permission("examinations.add_gradescale")
def scale_presets(request):
    """Start from a programme's scale instead of typing thresholds in by hand."""
    from .presets import install_preset, preset_choices, preset_notes

    if request.method == "POST":
        key = request.POST.get("preset", "")
        if key not in dict(preset_choices()):
            messages.error(request, "Choose one of the listed scales.")
        else:
            scale, created = install_preset(request.school, key)
            if created:
                messages.success(request, f"Added {scale.name}. Check the thresholds match your school's before use.")
            else:
                messages.info(request, f"{scale.name} is already set up; it was left as it is.")
            return redirect("examinations:rules", pk=scale.pk)
    notes = preset_notes()
    return render(
        request,
        "examinations/scale_presets.html",
        {
            "presets": [(key, name, notes[key]) for key, name in preset_choices()],
            "page_title": "Add a grade scale",
        },
    )


@require_permission("examinations.change_gradescale")
def grade_rules(request, pk):
    from django.forms import BaseInlineFormSet, inlineformset_factory

    class RuleFormSet(BaseInlineFormSet):
        def clean(self):
            super().clean()
            if any(self.errors):
                return
            ranges = sorted(
                (f.cleaned_data["min_percent"], f.cleaned_data["max_percent"])
                for f in self.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")
            )
            if not ranges or ranges[0][0] != 0 or ranges[-1][1] != 100:
                raise forms.ValidationError("Grading rules must cover 0 through 100 percent.")
            for previous, current in zip(ranges, ranges[1:], strict=False):
                if current[0] <= previous[1] or current[0] - previous[1] > Decimal("0.01"):
                    raise forms.ValidationError("Grade ranges cannot overlap or leave gaps.")

    scale = get_object_or_404(GradeScale, school=request.school, pk=pk)
    FormSet = inlineformset_factory(
        GradeScale,
        GradeRule,
        fields=["letter", "min_percent", "max_percent", "grade_point"],
        extra=1,
        can_delete=True,
        formset=RuleFormSet,
    )
    formset = FormSet(request.POST or None, instance=scale)
    if request.method == "POST" and formset.is_valid():
        formset.save()
        messages.success(request, "Grading rules saved. Published snapshots are unchanged.")
        return redirect("examinations:rules", pk=pk)
    return render(
        request, "examinations/rules.html", {"formset": formset, "page_title": "Grading rules: " + scale.name}
    )


@require_permission("examinations.view_exam", also="own sections only")
def admit_cards(request):
    """Printable admit cards for one section of an exam."""
    from .documents import admit_cards_pdf

    exam = get_object_or_404(Exam, school=request.school, pk=request.GET.get("exam", 0))
    section = get_object_or_404(Section, school=request.school, pk=request.GET.get("section", 0))
    if not may_use_section(request.user, request.school, section, exam.academic_year):
        raise PermissionDenied("You can print admit cards only for sections you taught that year.")
    schedules = list(
        exam.schedules.filter(class_level=section.class_level).select_related("subject").order_by("date", "start_time")
    )
    enrollments = list(
        Enrollment.objects.filter(school=request.school, academic_year=exam.academic_year, section=section)
        .select_related("student", "section__class_level")
        .prefetch_related("chosen_subjects")
        .order_by("roll_number")
    )
    from .subjects import papers_for

    plan = subject_plan(exam.academic_year, section.class_level)
    if request.school.public_results_enabled:
        from .public import ensure_codes

        ensure_codes([e.student for e in enrollments])
    # The same eligibility rule as mark entry and results: a card lists only that student's papers.
    return admit_cards_pdf(
        request.school,
        exam,
        enrollments,
        schedules,
        papers_for_student=lambda e: [schedule for schedule, _role in papers_for(e, schedules, plan)],
    )


@require_permission("examinations.view_exam")
def exam_routine(request, pk):
    """The paper timetable for an exam, on screen or as a PDF."""
    exam = get_object_or_404(Exam, school=request.school, pk=pk)
    schedules = exam.schedules.select_related("subject", "class_level").order_by(
        "date", "start_time", "class_level__order"
    )
    headers = ["Date", "Time", "Class", "Subject", "Full marks", "Pass marks", "Room"]
    rows = [
        [
            s.date or "To be announced",
            f"{s.start_time:%H:%M} - {s.end_time:%H:%M}" if s.start_time and s.end_time else "",
            str(s.class_level),
            str(s.subject),
            s.full_marks,
            s.pass_marks,
            s.room,
        ]
        for s in schedules
    ]
    fmt = request.GET.get("format")
    if fmt == "pdf":
        return table_document(
            request.school,
            f"Examination routine - {exam.name}",
            headers,
            rows,
            subtitle=str(exam.academic_year),
            filename=f"exam-routine-{exam.pk}.pdf",
        )
    if fmt in ("csv", "xlsx"):
        return spreadsheet(f"exam-routine-{exam.pk}", headers, rows, fmt)
    return render(
        request,
        "generic/report.html",
        {"page_title": f"Examination routine: {exam.name}", "headers": headers, "rows": rows, "form": None},
    )


@require_permission("examinations.view_mark", also="own sections, published only")
def report_cards(request):
    """Every card for a section in one PDF, for printing in a single run."""
    from .documents import bulk_report_cards_pdf

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
    sheet = build_result_sheet(exam, section.class_level, section)
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
            row,
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
    from .documents import progress_rows

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


def _typed_parts(post):
    """The parts rows as typed, skipping rows left entirely blank."""
    try:
        count = min(int(post.get("rows", "0")), 12)
    except ValueError:
        count = 0
    rows = []
    for index in range(count):
        row = {
            key: post.get(f"part-{index}-{key}", "").strip()
            for key in ("code", "name", "full_marks", "pass_marks", "weight")
        }
        if any(row.values()):
            rows.append(row)
    return rows


@require_permission("examinations.change_examschedule")
def paper_parts(request, pk):
    """
    A paper's parts: creative and multiple choice, weighted components, or MYP criteria.

    Presets fill the usual layouts in one step. Parts are fixed once marks are entered or
    results published, because they decide how every mark on the paper is totalled.
    """
    from .parts import PRESETS, apply_preset, locked_reason, plain, save_parts, scale_letters

    schedule = get_object_or_404(
        ExamSchedule.objects.select_related("exam__grade_scale", "class_level", "subject", "grade_scale"),
        school=request.school,
        pk=pk,
    )
    here = reverse("examinations:paper_parts", args=[schedule.pk])
    typed = None
    if request.method == "POST":
        try:
            if request.POST.get("action") == "preset":
                note = apply_preset(user=request.user, schedule=schedule, key=request.POST.get("preset", ""))
                messages.success(request, note)
            else:
                typed = _typed_parts(request.POST)
                saved = save_parts(user=request.user, schedule=schedule, parts=typed)
                messages.success(
                    request, f"Saved {len(saved)} part(s)." if saved else "Parts removed: the paper is a single score."
                )
            return redirect(here)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))

    parts = list(schedule.components.all())
    if typed is not None:
        rows = typed
    else:
        rows = [
            {
                "code": p.code,
                "name": p.name,
                "full_marks": plain(p.full_marks),
                "pass_marks": plain(p.pass_marks),
                "weight": plain(p.weight) if p.weight is not None else "",
            }
            for p in parts
        ]
    blank = {"code": "", "name": "", "full_marks": "", "pass_marks": "", "weight": ""}
    rows = rows + [dict(blank) for _ in range(max(2, 4 - len(rows)))]
    return render(
        request,
        "examinations/parts.html",
        {
            "schedule": schedule,
            "parts": parts,
            "rows": rows,
            "row_count": len(rows),
            "weighted": any(p.weight is not None for p in parts),
            "locked": locked_reason(schedule),
            "presets": [(key, label, note) for key, (label, note, _rows, _cap) in PRESETS.items()],
            "letters": scale_letters(schedule),
            "page_title": f"Parts · {schedule.subject} · {schedule.class_level}",
        },
    )


class OverallCommentFilter(TailwindFormMixin, forms.Form):
    exam = forms.ModelChoiceField(queryset=None)
    section = forms.ModelChoiceField(queryset=None)

    def __init__(self, *args, user, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["exam"].queryset = Exam.objects.filter(school=school).select_related("academic_year")
        self.fields["section"].queryset = sections_for(user, school).select_related("class_level")


def _comment_rows(enrollments, existing, typed, errors):
    rows = []
    for enrollment in enrollments:
        stored = existing.get(enrollment.pk)
        entry = typed.get(enrollment.pk)
        rows.append(
            {
                "enrollment": enrollment,
                "effort": entry["effort"] if entry else (stored.effort if stored else ""),
                "comment": entry["comment"] if entry else (stored.comment if stored else ""),
                "error": errors.get(enrollment.pk),
            }
        )
    return rows


@require_permission("examinations.change_resultcomment", also="assigned subjects and sections; class teachers overall")
def comments(request):
    """
    Written comments on results: per subject with an effort grade, for one paper in one
    section, or with ?overall=1 the class teacher's comment on each student's whole result.

    Fixed once the exam is published, and frozen into the published card.
    """
    from .feedback import (
        COMMENT_LIMIT,
        CommentError,
        effort_label,
        is_class_teacher,
        save_overall_comments,
        save_subject_comments,
    )
    from .models import ResultComment

    overall = (request.GET.get("overall") or request.POST.get("overall")) == "1"
    data = (
        request.POST
        if request.method == "POST"
        else (request.GET if ("exam" in request.GET or "schedule" in request.GET) else None)
    )
    if overall:
        selector = OverallCommentFilter(data, user=request.user, school=request.school)
    else:
        selector = MarkFilter(data, user=request.user, school=request.school)
    context = {"selector": selector, "overall": overall, "rows": [], "limit": COMMENT_LIMIT, "page_title": "Comments"}
    if not (selector.is_bound and selector.is_valid()):
        return render(request, "examinations/comments.html", context)

    section = selector.cleaned_data["section"]
    if overall:
        exam, schedule, subject = selector.cleaned_data["exam"], None, None
        if section.class_level_id not in set(exam.schedules.values_list("class_level_id", flat=True)):
            messages.error(request, f"{exam} has no papers for {section.class_level}.")
            return render(request, "examinations/comments.html", context)
        if not (is_manager(request.user) or is_class_teacher(request.user, section)):
            raise PermissionDenied("Only the class teacher or the school's managers write overall comments.")
    else:
        schedule = selector.cleaned_data["schedule"]
        exam, subject = schedule.exam, schedule.subject
        assert_can_mark(request.user, schedule, section=section, permission="examinations.change_resultcomment")
    enrollments = list(
        Enrollment.objects.filter(school=request.school, academic_year=exam.academic_year, section=section)
        .select_related("student")
        .prefetch_related("chosen_subjects")
        .order_by("roll_number")
    )
    if schedule is not None:
        enrollments = enrollments_taking(schedule, enrollments, subject_plan(exam.academic_year, schedule.class_level))
    existing = {
        c.enrollment_id: c for c in ResultComment.objects.filter(exam=exam, subject=subject, enrollment__in=enrollments)
    }
    typed, errors = {}, {}
    locked = exam.publication_version > 0
    if request.method == "POST":
        submitted = []
        for enrollment in enrollments:
            effort = request.POST.get(f"{enrollment.pk}-effort", "")
            comment = request.POST.get(f"{enrollment.pk}-comment", "")
            typed[enrollment.pk] = {"effort": effort, "comment": comment}
            submitted.append((enrollment, effort, comment))
        try:
            if overall:
                saved = save_overall_comments(user=request.user, exam=exam, section=section, rows=submitted)
                query = {"overall": 1, "exam": exam.pk, "section": section.pk}
            else:
                saved = save_subject_comments(user=request.user, schedule=schedule, section=section, rows=submitted)
                query = {"schedule": schedule.pk, "section": section.pk}
            messages.success(request, f"Saved {saved} change(s)." if saved else "Nothing had changed.")
            return redirect(reverse("examinations:comments") + "?" + urlencode(query))
        except CommentError as exc:
            errors = exc.errors
            messages.error(request, "Nothing was saved. Fix the rows marked below and save again.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    context.update(
        {
            "exam": exam,
            "schedule": schedule,
            "section": section,
            "locked": locked,
            "effort_label": effort_label(exam.rules_for(section.class_level)),
            "rows": _comment_rows(enrollments, existing, typed, errors),
        }
    )
    return render(request, "examinations/comments.html", context)


class ForecastFilter(TailwindFormMixin, forms.Form):
    section = forms.ModelChoiceField(queryset=None)
    subject = forms.ModelChoiceField(queryset=None)
    kind = forms.ChoiceField(choices=())

    def __init__(self, *args, user, school, **kwargs):
        from academics.models import Subject

        from .models import GradeForecast

        super().__init__(*args, **kwargs)
        self.fields["section"].queryset = sections_for(user, school).select_related("class_level")
        self.fields["subject"].queryset = Subject.objects.filter(school=school)
        self.fields["kind"].choices = GradeForecast.Kind.choices


@require_permission("examinations.add_gradeforecast", also="subjects taught in the section; managers approve")
def forecasts(request):
    """
    Predicted, forecast and target grades for one subject in one section.

    Every entry is a new dated record, so the history stays. A teacher's entries wait for a
    manager's approval before any card shows them; a manager's are approved as made.
    """
    from datetime import date as _date

    from django.utils import timezone

    from academics.models import AcademicYear

    from .feedback import approve_forecasts, may_forecast, record_forecasts
    from .models import GradeForecast
    from .subjects import paper_role

    data = request.POST if request.method == "POST" else (request.GET if "section" in request.GET else None)
    selector = ForecastFilter(data, user=request.user, school=request.school)
    year = AcademicYear.current_for(request.school)
    context = {"selector": selector, "rows": [], "year": year, "page_title": "Grade estimates"}
    if not (selector.is_bound and selector.is_valid()) or year is None:
        return render(request, "examinations/forecasts.html", context)
    section, subject, kind = (selector.cleaned_data[k] for k in ("section", "subject", "kind"))
    if not may_forecast(request.user, subject, section, year):
        raise PermissionDenied("You can record grades only for subjects you teach in this section.")
    plan = subject_plan(year, section.class_level)
    enrollments = [
        e
        for e in Enrollment.objects.filter(school=request.school, academic_year=year, section=section)
        .select_related("student")
        .prefetch_related("chosen_subjects")
        .order_by("roll_number")
        if paper_role(e, subject, plan)
    ]
    here = (
        reverse("examinations:forecasts")
        + "?"
        + urlencode({"section": section.pk, "subject": subject.pk, "kind": kind})
    )
    typed = {}
    as_of_raw = request.POST.get("as_of", "") if request.method == "POST" else ""
    if request.method == "POST":
        try:
            if request.POST.get("action") == "approve":
                count = approve_forecasts(
                    user=request.user, section=section, subject=subject, academic_year=year, kind=kind
                )
                messages.success(request, f"Approved {count} record(s).")
                return redirect(here)
            typed = {e.pk: request.POST.get(f"{e.pk}-grade", "") for e in enrollments}
            try:
                as_of = _date.fromisoformat(as_of_raw)
            except ValueError:
                as_of = None
            count = record_forecasts(
                user=request.user,
                section=section,
                subject=subject,
                academic_year=year,
                kind=kind,
                as_of=as_of,
                rows=[(e, typed[e.pk]) for e in enrollments],
            )
            messages.success(request, f"Recorded {count} grade(s)." if count else "No grades were entered.")
            return redirect(here)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    records = GradeForecast.objects.filter(enrollment__in=enrollments, subject=subject, kind=kind).select_related(
        "recorded_by"
    )
    approved, pending = {}, {}
    for record in records:  # newest first
        bucket = approved if record.approved_at else pending
        bucket.setdefault(record.enrollment_id, record)
    context.update(
        {
            "section": section,
            "subject": subject,
            "kind_label": dict(GradeForecast.Kind.choices)[kind],
            "as_of": as_of_raw or timezone.localdate().isoformat(),
            "can_approve": is_manager(request.user) and request.user.has_perm("examinations.change_gradeforecast"),
            "pending_count": sum(1 for e in enrollments if e.pk in pending),
            "rows": [
                {
                    "enrollment": e,
                    "approved": approved.get(e.pk),
                    "pending": pending.get(e.pk),
                    "typed": typed.get(e.pk, ""),
                }
                for e in enrollments
            ],
        }
    )
    return render(request, "examinations/forecasts.html", context)


# ------------------------------------------------------------------ exam series and official results

OFFICIAL_IMPORT_KEY = "official_results_import"


@require_permission("examinations.view_examseries")
def series_list(request):
    from .models import ExamSeries

    series = ExamSeries.objects.filter(school=request.school).annotate(
        candidate_count=Count("candidates", distinct=True)
    )
    return render(request, "examinations/series_list.html", {"series": series, "page_title": "Exam series"})


def _series(request, pk):
    from .models import ExamSeries

    return get_object_or_404(ExamSeries, school=request.school, pk=pk)


@require_permission("examinations.view_examseries")
def series_detail(request, pk):
    """
    Candidates and entries for one series: register a section's students, correct candidate
    numbers, enter candidates for a syllabus, and withdraw entries (which are kept).
    """
    from academics.models import Section, Subject
    from students.models import Student

    from .models import SeriesCandidate, SeriesEntry
    from .official import add_candidates, add_entries, update_candidate, withdraw_entry

    series = _series(request, pk)
    here = reverse("examinations:series_detail", args=[series.pk])
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "add_candidates":
                section = get_object_or_404(Section, school=request.school, pk=request.POST.get("section") or 0)
                start = request.POST.get("first_number", "").strip()
                from academics.models import AcademicYear

                students = list(
                    Student.objects.filter(
                        school=request.school,
                        enrollments__section=section,
                        enrollments__academic_year=AcademicYear.current_for(request.school),
                        enrollments__status=Enrollment.Status.ENROLLED,
                    ).distinct()
                )
                added = add_candidates(
                    user=request.user,
                    series=series,
                    students=students,
                    first_number=int(start) if start.isdigit() else None,
                )
                messages.success(request, f"Registered {added} candidate(s) from {section}.")
            elif action == "update_candidate":
                candidate = get_object_or_404(SeriesCandidate, series=series, pk=request.POST.get("candidate") or 0)
                update_candidate(
                    user=request.user,
                    candidate=candidate,
                    candidate_number=request.POST.get("candidate_number", ""),
                    uci=request.POST.get("uci", ""),
                    certificate_name=request.POST.get("certificate_name"),
                )
                messages.success(request, f"Updated {candidate.student}.")
            elif action == "add_entries":
                chosen = [pk for pk in request.POST.getlist("candidates") if pk.isdigit()]
                candidates = list(SeriesCandidate.objects.filter(series=series, pk__in=chosen))
                if not candidates:
                    raise ValidationError("Tick the candidates to enter.")
                raw_subject = request.POST.get("subject", "")
                subject = (
                    get_object_or_404(Subject, school=request.school, pk=raw_subject) if raw_subject.isdigit() else None
                )
                made, skipped = add_entries(
                    user=request.user,
                    series=series,
                    candidates=candidates,
                    qualification=request.POST.get("qualification", ""),
                    syllabus_code=request.POST.get("syllabus_code", ""),
                    syllabus_title=request.POST.get("syllabus_title", ""),
                    option_code=request.POST.get("option_code", ""),
                    tier=request.POST.get("tier", ""),
                    level=request.POST.get("level", ""),
                    subject=subject,
                )
                note = f" ({skipped} already entered)" if skipped else ""
                messages.success(request, f"Entered {made} candidate(s){note}.")
            elif action == "arrangement":
                from .candidates import save_arrangement
                from .models import AccessArrangement

                candidate = get_object_or_404(SeriesCandidate, series=series, pk=request.POST.get("candidate") or 0)
                raw = request.POST.get("arrangement", "")
                existing = (
                    get_object_or_404(AccessArrangement, candidate__series=series, pk=raw) if raw.isdigit() else None
                )
                raw_consent = request.POST.get("consent_on", "")
                try:
                    consent = date.fromisoformat(raw_consent) if raw_consent else None
                except ValueError:
                    raise ValidationError("Enter the consent date as a date.") from None
                save_arrangement(
                    user=request.user,
                    candidate=candidate,
                    kind=request.POST.get("kind", ""),
                    details=request.POST.get("details", ""),
                    evidence=request.POST.get("evidence", ""),
                    consent_on=consent,
                    status=request.POST.get("status", "draft"),
                    arrangement=existing,
                )
                messages.success(request, f"Saved the access arrangement for {candidate.student}.")
            elif action == "withdraw":
                entry = get_object_or_404(SeriesEntry, candidate__series=series, pk=request.POST.get("entry") or 0)
                withdraw_entry(user=request.user, entry=entry)
                messages.success(request, f"Withdrew {entry.syllabus_code} for {entry.candidate.student}.")
            return redirect(here)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    candidates = series.candidates.select_related("student").prefetch_related("entries")
    from .candidates import entry_problems
    from .models import AccessArrangement

    sees_arrangements = is_manager(request.user) and request.user.has_perm("examinations.view_accessarrangement")
    if sees_arrangements:
        from students.privacy import log_sensitive

        log_sensitive(request, "Access arrangements", str(series))
    return render(
        request,
        "examinations/series_detail.html",
        {
            "series": series,
            "candidates": candidates,
            "problems": entry_problems(series),
            "arrangements": (
                AccessArrangement.objects.filter(candidate__series=series).select_related("candidate__student")
                if sees_arrangements
                else None
            ),
            "arrangement_kinds": AccessArrangement.Kind.choices,
            "arrangement_statuses": AccessArrangement.Status.choices,
            "sections": sections_for(request.user, request.school).select_related("class_level"),
            "subjects": Subject.objects.filter(school=request.school),
            "tiers": SeriesEntry.Tier.choices,
            "page_title": str(series),
        },
    )


@require_permission("examinations.view_officialresult")
def series_results(request, pk):
    """
    Official results for a series: import the body's statement of results as a CSV (checked
    and shown before anything is written), confirm them, and see amendments.
    """
    from datetime import date as _date

    from .models import OfficialResult
    from .official import check_results, confirm_results, import_results, read_result_rows

    series = _series(request, pk)
    here = reverse("examinations:series_results", args=[series.pk])
    preview = None
    session_key = f"{OFFICIAL_IMPORT_KEY}:{series.pk}"
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "check" and request.FILES.get("file"):
                if not request.user.has_perm("examinations.add_officialresult"):
                    raise PermissionDenied
                prepared, errors = check_results(series, read_result_rows(request.FILES["file"]))
                preview = {
                    "rows": prepared,
                    "errors": errors,
                    "new": sum(r["action"] == "new" for r in prepared),
                    "amend": sum(r["action"] == "amend" for r in prepared),
                    "unchanged": sum(r["action"] == "unchanged" for r in prepared),
                    "unentered": sum(not r["entered"] for r in prepared),
                }
                if errors:
                    request.session.pop(session_key, None)
                else:
                    request.session[session_key] = prepared
            elif action == "import":
                prepared = request.session.get(session_key)
                if not prepared:
                    raise ValidationError("Check a file first; nothing is waiting to be imported.")
                try:
                    received = _date.fromisoformat(request.POST.get("received_on", ""))
                except ValueError:
                    received = None
                added, amended = import_results(
                    user=request.user,
                    series=series,
                    prepared=prepared,
                    source=request.POST.get("source", ""),
                    received_on=received,
                    amendment_reason=request.POST.get("amendment_reason", ""),
                )
                request.session.pop(session_key, None)
                messages.success(
                    request, f"Recorded {added} new and {amended} amended result(s). Another manager must confirm them."
                )
                return redirect(here)
            elif action == "confirm":
                count, own = confirm_results(user=request.user, series=series)
                note = f" {own} you imported yourself need another manager to confirm." if own else ""
                messages.success(request, f"Confirmed {count} result(s).{note}")
                return redirect(here)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    results = (
        OfficialResult.objects.filter(candidate__series=series)
        .select_related("candidate__student", "recorded_by", "checked_by", "supersedes")
        .order_by("candidate__candidate_number", "syllabus_code", "-created_at")
    )
    waiting = results.filter(is_current=True, checked_at__isnull=True).count()
    return render(
        request,
        "examinations/series_results.html",
        {
            "series": series,
            "results": results,
            "preview": preview,
            "waiting": waiting,
            "today": timezone.localdate().isoformat(),
            "page_title": f"Official results · {series}",
        },
    )


# ------------------------------------------------------------------ combined results


def _as_exam(combined):
    """What the report card needs to know about "the exam" when the result is a combination."""
    from types import SimpleNamespace

    return SimpleNamespace(
        pk=combined.pk,
        name=combined.name,
        academic_year=combined.academic_year,
        status=combined.status,
        end_date=None,
    )


@require_permission("examinations.view_combinedresult")
def combined_list(request):
    """Combined results, and a form to set one up from the year's exams with weights."""
    from academics.models import AcademicYear

    from .combined import save_combined
    from .models import CombinedResult

    years = AcademicYear.objects.filter(school=request.school)
    raw_year = request.POST.get("academic_year") or request.GET.get("year") or ""
    year = (
        years.filter(pk=int(raw_year)).first() if str(raw_year).isdigit() else AcademicYear.current_for(request.school)
    )
    exams = (
        Exam.objects.filter(school=request.school, academic_year=year).order_by("start_date", "name") if year else []
    )
    typed = {}
    if request.method == "POST":
        typed = {key: request.POST.get(key, "") for key in request.POST}
        try:
            scale = get_object_or_404(GradeScale, school=request.school, pk=request.POST.get("grade_scale") or 0)
            parts = []
            for exam in exams:
                raw = request.POST.get(f"weight-{exam.pk}", "").strip()
                if raw:
                    try:
                        parts.append((exam, Decimal(raw)))
                    except InvalidOperation:
                        raise ValidationError(f"{exam.name}: '{raw}' is not a number.") from None
            combined = save_combined(
                user=request.user,
                school=request.school,
                academic_year=year,
                name=request.POST.get("name", ""),
                grade_scale=scale,
                parts=parts,
            )
            messages.success(request, f"Set up {combined.name}.")
            return redirect("examinations:combined_detail", pk=combined.pk)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    return render(
        request,
        "examinations/combined_list.html",
        {
            "combined": CombinedResult.objects.filter(school=request.school).select_related("academic_year"),
            "years": years,
            "year": year,
            "exams": [(exam, typed.get(f"weight-{exam.pk}", "")) for exam in exams],
            "scales": GradeScale.objects.filter(school=request.school),
            "typed": typed,
            "page_title": "Combined results",
        },
    )


@require_permission("examinations.view_combinedresult")
def combined_detail(request, pk):
    from .combined import class_levels, combined_sheet, out_of_date, publish_combined
    from .models import CombinedResult

    combined = get_object_or_404(
        CombinedResult.objects.select_related("academic_year", "grade_scale"), school=request.school, pk=pk
    )
    if request.method == "POST":
        try:
            publish_combined(combined, request.user)
            messages.success(request, f"Published {combined.name}.")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        return redirect("examinations:combined_detail", pk=combined.pk)
    levels = list(class_levels(combined))
    raw = request.GET.get("class_level", "")
    level = next((lv for lv in levels if str(lv.pk) == raw), levels[0] if levels else None)
    rows, problem = [], ""
    if level is not None:
        try:
            rows = combined_sheet(combined, level)
        except ValidationError as exc:
            problem = " ".join(exc.messages)
    book_label = rows[0]["rulebook"] if rows else ""
    if request.GET.get("format") == "csv" and rows:
        headers = ["Section", "Roll", "Student", "Total", "Percent", "GPA", "Overall"]
        body = [
            [r["section"], r["roll"], r["student"], r["total"], r["percent"] or "", r["gpa"] or "", headline(r)]
            for r in rows
        ]
        preamble = [
            ("Combined result", str(combined)),
            ("Class", str(level)),
            (
                "Version",
                f"Published version {combined.publication_version}"
                if combined.publication_version
                else "Draft: not published",
            ),
            ("Made from", ", ".join(f"{p.exam.name} {p.weight}%" for p in combined.parts.select_related("exam"))),
            ("Rulebook", book_label),
        ]
        return spreadsheet("combined-result", headers, body, "csv", preamble=preamble)
    return render(
        request,
        "examinations/combined_detail.html",
        {
            "combined": combined,
            "parts": combined.parts.select_related("exam"),
            "levels": levels,
            "level": level,
            "rows": rows,
            "problem": problem,
            "stale": out_of_date(combined),
            "can_publish": is_manager(request.user) and request.user.has_perm("examinations.change_combinedresult"),
            "page_title": str(combined),
        },
    )


@require_permission(None, also="managers; teachers of the section that year; the child's own family")
def combined_card(request, pk, student_pk):
    """A student's combined result card, from the published version, with the same access rules as an exam card."""
    from core.qr import qr_svg

    from .combined import combined_sheet, out_of_date
    from .documents import card_rows, shows_points
    from .models import CombinedResult, CombinedSnapshot

    combined = get_object_or_404(CombinedResult, school=request.school, pk=pk)
    exam_like = _as_exam(combined)
    student, enr = card_enrollment(request, exam_like, student_pk)
    if combined.status != "published" and not request.user.has_perm("examinations.view_mark"):
        raise PermissionDenied
    row = next((r for r in combined_sheet(combined, enr.class_level) if r["enrollment_id"] == enr.pk), None)
    if row is None:
        from django.http import Http404

        raise Http404
    snap = CombinedSnapshot.objects.filter(
        combined=combined, enrollment=enr, version=combined.publication_version
    ).first()
    link = (
        request.build_absolute_uri(reverse("examinations:combined_verify", args=[snap.verification_code]))
        if snap
        else ""
    )
    if request.GET.get("format") == "pdf":
        from .documents import report_card_pdf

        return report_card_pdf(request.school, exam_like, enr, row, snap, verify_url=link)
    lines = card_rows(row)
    staff = request.user.has_perm("examinations.view_mark")
    return render(
        request,
        "examinations/report_card.html",
        {
            "row": row,
            "exam": exam_like,
            "snapshot": snap,
            "lines": lines,
            "show_parts": False,
            "show_points": shows_points(row),
            "attendance": row.get("attendance"),
            "attendance_until": None,
            "show_effort": False,
            "comment_span": 4 + shows_points(row),
            "effort_label": "Effort",
            "official_notice": official_notice(row),
            "board": row.get("system") == "national",
            "trace": row.get("trace") if staff else None,
            "stale": out_of_date(combined) if staff else [],
            "verify_url": link,
            "qr": qr_svg(link) if link else "",
            "page_title": "Combined result",
        },
    )


def combined_verify(request, code):
    """Public check of a combined result card, showing no more than the exam card's check."""
    from .models import CombinedSnapshot

    snapshot = get_object_or_404(
        CombinedSnapshot.objects.select_related("combined__academic_year", "school"), verification_code=code
    )
    combined = snapshot.combined
    payload = snapshot.payload or {}
    return render(
        request,
        "examinations/verify.html",
        {
            "snapshot": snapshot,
            "school": snapshot.school,
            "exam": _as_exam(combined),
            "current": combined.status == "published" and snapshot.version == combined.publication_version,
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


@require_permission("examinations.view_examseries")
def series_export(request, pk):
    """The series' entries as a file for the exam officer to check and upload to the body's own system."""
    from .candidates import entry_rows

    series = _series(request, pk)
    headers, rows = entry_rows(series)
    fmt = "xlsx" if request.GET.get("format") == "xlsx" else "csv"
    preamble = [
        ("Series", str(series)),
        ("Centre", series.centre_number or "not set"),
        ("Entries", str(len(rows))),
        ("Note", "Check against the body's entry system before uploading; this file is not sent anywhere."),
    ]
    return spreadsheet(f"entries-{series.pk}", headers, rows, fmt, preamble=preamble)


@require_permission("students.view_student", also="managers only: identity numbers of students and parents")
def board_registration(request):
    """
    The national board registration (eSIF) fields for a class, with the gaps to fill first.

    It carries birth registration and parents' NID numbers, so only the school's managers
    can open it.
    """
    from academics.models import AcademicYear

    from .candidates import registration_rows

    if not is_manager(request.user):
        raise PermissionDenied("Only the school's managers can export registration data.")
    year = AcademicYear.current_for(request.school)
    levels = ClassLevel.objects.filter(school=request.school, is_active=True).order_by("order")
    raw = request.GET.get("class_level", "")
    level = levels.filter(pk=int(raw)).first() if raw.isdigit() else None
    headers, rows, checks = registration_rows(year, level) if (year and level) else ([], [], [])
    if level:
        from students.privacy import log_sensitive

        log_sensitive(
            request,
            "Board registration data",
            f"{level} ({request.GET.get('format') or 'screen'})",
        )
    if level and request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet(
            f"board-registration-{level.pk}",
            headers,
            rows,
            request.GET["format"],
            preamble=[
                ("Class", str(level)),
                ("Year", str(year)),
                ("Students", str(len(rows))),
                ("Gaps to fill", str(len(checks))),
            ],
        )
    return render(
        request,
        "examinations/board_registration.html",
        {
            "levels": levels,
            "level": level,
            "year": year,
            "headers": headers,
            "rows": rows,
            "checks": checks,
            "page_title": "Board registration data",
        },
    )


def public_results(request, slug):
    """
    Families look up a published result without signing in, when the school allows it.

    The student ID and the result code from the admit card must both match. A wrong pair
    says only that nothing matched, and too many from one address are turned away.
    """
    from core.models import School

    from .public import lookup, published_results, throttled

    school = get_object_or_404(School, slug=slug, is_active=True)
    if not school.public_results_enabled:
        from django.http import Http404

        raise Http404
    address = request.META.get("REMOTE_ADDR", "")
    context = {"school": school, "student": None, "error": "", "page_title": "Results"}
    if request.method == "POST":
        if throttled(address, school):
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
