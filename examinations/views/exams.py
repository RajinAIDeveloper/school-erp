"""
Exams themselves: the list, the exam page and publishing, papers and their parts, grading scales, unlock requests, admit cards and the exam routine.
"""

from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from academics.models import Section
from core.access import is_manager, may_use_section, require_permission, sections_for
from core.exports import spreadsheet
from core.generic import ERPListView
from core.pdf import table_document
from students.models import Enrollment

from ..models import Exam, ExamSchedule, GradeRule, GradeScale, Mark, UnlockRequest
from ..services import (
    assert_can_mark,
    expected_marks,
    publish_exam,
    review_unlock,
)
from ..subjects import subject_plan


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
    from ..clashes import timetable_clashes

    return timetable_clashes(exam)


def _checklist_for(user, exam):
    """The pre-publication checks, for the people who can publish, while it is still a draft."""
    if exam.status == "published" or not (is_manager(user) and user.has_perm("examinations.change_exam")):
        return None
    from ..checklist import publication_checklist

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
    from ..presets import install_preset, preset_choices, preset_notes

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
    from ..documents import admit_cards_pdf

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
    from ..subjects import papers_for

    plan = subject_plan(exam.academic_year, section.class_level)
    if request.school.public_results_enabled:
        from ..public import ensure_codes

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
    from ..parts import PRESETS, apply_preset, locked_reason, plain, save_parts, scale_letters

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
