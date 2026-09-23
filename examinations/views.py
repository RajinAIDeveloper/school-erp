from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from academics.models import ClassLevel, Section
from core.access import is_manager, may_use_section, require_permission, sections_for, students_for, taught_sections
from core.exports import spreadsheet
from core.forms import TailwindFormMixin
from core.generic import ERPListView
from core.pdf import table_document
from students.models import Enrollment

from .models import Exam, ExamSchedule, GradeRule, GradeScale, Mark, ResultSnapshot, UnlockRequest
from .services import (
    MarkEntryError,
    active_unlock,
    assert_can_mark,
    build_result_sheet,
    publish_exam,
    review_unlock,
    save_mark,
    save_marks,
)


def verification_url(request, snapshot):
    """
    The address printed on a report card, in full.

    A code on its own is no use to the employer or college holding the card: they have to
    know where to type it. This is the whole link, so it can be read off paper.
    """
    if snapshot is None:
        return ""
    return request.build_absolute_uri(reverse("examinations:verify", args=[snapshot.verification_code]))


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
    expected = sum(
        Enrollment.objects.filter(
            school=request.school, academic_year=exam.academic_year, class_level=s.class_level
        ).count()
        for s in schedules
    )
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
        },
    )


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

    if selector.is_bound and selector.is_valid():
        schedule = selector.cleaned_data["schedule"]
        section = selector.cleaned_data["section"]
        assert_can_mark(request.user, schedule)
        locked = schedule.exam.status == "published" and not active_unlock(request.user, schedule)
        enrollments = list(
            Enrollment.objects.filter(school=request.school, academic_year=schedule.exam.academic_year, section=section)
            .select_related("student")
            .order_by("roll_number")
        )
        existing = {
            mark.enrollment_id: mark for mark in Mark.objects.filter(schedule=schedule, enrollment__in=enrollments)
        }

        if request.method == "POST":
            if locked:
                raise PermissionDenied("Results are published. Request an unlock before editing.")
            if not request.user.has_perm("examinations.change_mark"):
                raise PermissionDenied
            submitted, invalid = [], {}
            for enrollment in enrollments:
                prefix = str(enrollment.pk)
                raw = request.POST.get(f"{prefix}-score", "").strip()
                absent = request.POST.get(f"{prefix}-absent") == "on"
                try:
                    version = int(request.POST.get(f"{prefix}-version", "0"))
                except ValueError:
                    version = 0
                score = None
                if raw and not absent:
                    try:
                        score = Decimal(raw)
                    except InvalidOperation:
                        invalid[enrollment.pk] = f"'{raw}' is not a number."
                        continue
                submitted.append((enrollment, score, absent, version))
            if invalid:
                row_errors = invalid
            else:
                try:
                    saved = save_marks(user=request.user, schedule=schedule, section=section, rows=submitted)
                    messages.success(request, f"Saved {len(saved)} mark(s).")
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
            rows.append(
                {
                    "enrollment": enrollment,
                    "mark": mark,
                    "score": mark.marks_obtained if mark and not mark.is_absent else None,
                    "absent": bool(mark and mark.is_absent),
                    "version": mark.version if mark else 0,
                    "error": row_errors.get(enrollment.pk),
                }
            )

    entered = sum(1 for row in rows if row["score"] is not None or row["absent"])
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


class ResultsFilter(TailwindFormMixin, forms.Form):
    term = forms.ModelChoiceField(
        queryset=None, required=False, help_text="Narrows the exam list to one term of the year."
    )
    exam = forms.ModelChoiceField(queryset=None)
    class_level = forms.ModelChoiceField(queryset=None)
    section = forms.ModelChoiceField(queryset=None, required=False, help_text="Leave blank for a class-wide sheet.")

    def __init__(self, *args, user, school, **kwargs):
        super().__init__(*args, **kwargs)
        from academics.models import Term

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
    sheet = None
    if form.is_bound and form.is_valid():
        chosen = {key: value for key, value in form.cleaned_data.items() if key != "term"}
        sheet = build_result_sheet(**chosen)
        fmt = request.GET.get("format")
        if fmt in ("csv", "xlsx", "pdf"):
            subjects = [c["subject"] for c in sheet["rows"][0]["cells"]] if sheet["rows"] else []
            headers = [
                "Class rank",
                "Section rank",
                "Section",
                "Roll",
                "Student",
                *subjects,
                "Total",
                "Percent",
                "GPA",
                "Result",
            ]
            rows = [
                [
                    r["grade_rank"],
                    r["rank"],
                    r["section"],
                    r["roll"],
                    r["student"],
                    *["ABS" if c["absent"] else (c["score"] if not c["missing"] else "") for c in r["cells"]],
                    r["total"],
                    r["percent"],
                    r["gpa"],
                    r["result"],
                ]
                for r in sheet["rows"]
            ]
            if fmt == "pdf":
                subtitle = f"{sheet['class_level']}"
                if sheet["section"]:
                    subtitle += f" - {sheet['section']}"
                return table_document(
                    request.school,
                    f"Result sheet - {sheet['exam'].name}",
                    headers,
                    rows,
                    subtitle=subtitle,
                    filename="result-sheet.pdf",
                )
            stats = sheet["subject_stats"]
            return spreadsheet(
                "results",
                headers,
                rows,
                fmt,
                extra_sheets=[
                    (
                        "Subject analysis",
                        [
                            "Subject",
                            "Entered",
                            "Absent",
                            "Missing",
                            "Average",
                            "Highest",
                            "Lowest",
                            "Failed",
                            "Pass percent",
                        ],
                        [
                            [
                                s[k]
                                for k in (
                                    "subject",
                                    "entered",
                                    "absent",
                                    "missing",
                                    "average",
                                    "highest",
                                    "lowest",
                                    "failed",
                                    "pass_pct",
                                )
                            ]
                            for s in stats
                        ],
                    )
                ],
            )
    return render(
        request,
        "examinations/results.html",
        {"form": form, "sheet": sheet, "page_title": "Results and subject analysis"},
    )


@require_permission(None)
def report_card(request, exam_pk, student_pk):
    exam = get_object_or_404(Exam, pk=exam_pk, school=request.school)
    student = get_object_or_404(students_for(request.user, request.school), pk=student_pk)
    if not request.user.has_perm("examinations.view_mark") and exam.status != "published":
        raise PermissionDenied
    enr = get_object_or_404(Enrollment, student=student, academic_year=exam.academic_year)
    sheet = build_result_sheet(exam, enr.class_level, enr.section)
    row = next((r for r in sheet["rows"] if r["enrollment_id"] == enr.pk), None)
    if row is None:
        from django.http import Http404

        raise Http404
    snap = ResultSnapshot.objects.filter(exam=exam, enrollment=enr, version=exam.publication_version).first()
    if request.GET.get("format") == "pdf":
        from .documents import report_card_pdf

        return report_card_pdf(request.school, exam, enr, row, snap, verify_url=verification_url(request, snap))
    return render(
        request,
        "examinations/report_card.html",
        {"row": row, "exam": exam, "snapshot": snap, "page_title": "Report card"},
    )


def verify(request, code):
    """
    Public check that a report card is genuine.

    It confirms the school, the version and whether that version is still current, and
    deliberately names no child and no mark: anyone may be holding the code.
    """
    snapshot = get_object_or_404(
        ResultSnapshot.objects.select_related("exam__academic_year", "school"), verification_code=code
    )
    current = snapshot.exam.status == "published" and snapshot.version == snapshot.exam.publication_version
    return render(
        request,
        "examinations/verify.html",
        {
            "snapshot": snapshot,
            "school": snapshot.school,
            "exam": snapshot.exam,
            "current": current,
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
        .order_by("roll_number")
    )
    return admit_cards_pdf(request.school, exam, enrollments, schedules)


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
    headers = ["Examination", "Class", "Total", "Percent", "GPA", "Rank", "Result"]
    table = [[r["exam"].name, r["section"], r["total"], r["percent"], r["gpa"], r["rank"], r["result"]] for r in rows]
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
