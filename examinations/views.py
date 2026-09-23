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

from .grading import headline
from .models import Exam, ExamSchedule, GradeRule, GradeScale, Mark, ResultSnapshot, UnlockRequest
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

        if request.method == "POST":
            if locked:
                raise PermissionDenied("Results are published. Request an unlock before editing.")
            if not request.user.has_perm("examinations.change_mark"):
                raise PermissionDenied
            submitted, invalid = [], {}
            for enrollment in enrollments:
                prefix = str(enrollment.pk)
                absent = request.POST.get(f"{prefix}-absent") == "on"
                typed[enrollment.pk] = {
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
                else:
                    raw = request.POST.get(f"{prefix}-score", "").strip()
                    if raw and not absent:
                        try:
                            score = Decimal(raw)
                        except InvalidOperation:
                            invalid[enrollment.pk] = f"'{raw}' is not a number."
                            continue
                submitted.append((enrollment, score, absent, version, parts))
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
                    "version": mark.version if mark else 0,
                    "parts": [
                        (component, entry["parts"][component.code] if entry else stored_parts.get(component.code, ""))
                        for component in components
                    ],
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
            "components": components,
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
        book = sheet["rulebook"]
        show_rank = book.show_rank(sheet["exam"])
        if fmt in ("csv", "xlsx", "pdf"):
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
        {
            "form": form,
            "sheet": sheet,
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

    from .documents import attendance_for, card_rows

    lines = card_rows(row)
    link = verification_url(request, snap)
    return render(
        request,
        "examinations/report_card.html",
        {
            "row": row,
            "exam": exam,
            "snapshot": snap,
            "lines": lines,
            "show_parts": any(line["parts"] for line in lines),
            "attendance": row["attendance"] if "attendance" in row else attendance_for(enr, exam.end_date),
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
