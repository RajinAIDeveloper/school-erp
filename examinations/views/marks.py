"""
Entering marks, and the words and estimates that go on a card beside them.
"""

from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import is_manager, require_permission, sections_for
from core.forms import TailwindFormMixin
from students.models import Enrollment

from ..models import Exam, ExamSchedule, Mark
from ..parts import plain
from ..services import (
    MarkEntryError,
    active_unlock,
    assert_can_mark,
    save_mark,
    save_marks,
)
from ..subjects import enrollments_taking, subject_plan


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


def _students_sitting(request, schedule, section):
    """The section's students who sit this paper in the exam's year, in roll order."""
    return enrollments_taking(
        schedule,
        list(
            Enrollment.objects.filter(school=request.school, academic_year=schedule.exam.academic_year, section=section)
            .select_related("student")
            .prefetch_related("chosen_subjects")
            .order_by("roll_number")
        ),
        subject_plan(schedule.exam.academic_year, schedule.class_level),
    )


def _paper_and_class(request, data):
    """
    The paper and section named in `data`, checked exactly as the mark grid checks them, with
    the students who sit it and the marks already entered.
    """
    selector = MarkFilter(data, user=request.user, school=request.school)
    if not selector.is_valid():
        raise Http404("Choose an exam paper and a section.")
    schedule, section = selector.cleaned_data["schedule"], selector.cleaned_data["section"]
    assert_can_mark(request.user, schedule, section=section)
    students = _students_sitting(request, schedule, section)
    existing = {mark.enrollment_id: mark for mark in Mark.objects.filter(schedule=schedule, enrollment__in=students)}
    return schedule, section, students, existing


def _draft_base(rows):
    import hashlib

    state = ",".join(f"{row['enrollment'].pk}:{row['version']}" for row in rows)
    return hashlib.sha256(state.encode()).hexdigest()[:16]


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
        enrollments = _students_sitting(request, schedule, section)
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
            # Changes whenever a stored mark does, so a draft typed over older marks is dropped.
            "draft_base": _draft_base(rows),
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


@require_permission("examinations.view_mark", also="assigned subjects and sections only")
def mark_sheet(request):
    """The exam-hall sheet for a paper and section: blank to fill by hand, or the marks register."""
    from ..documents import mark_sheet_pdf

    schedule, section, students, existing = _paper_and_class(request, request.GET)
    filled = request.GET.get("kind") == "register"
    return mark_sheet_pdf(request.school, schedule, section, students, existing if filled else None)


@require_permission("examinations.change_mark", also="assigned subjects and sections only")
def marks_import(request):
    """
    Marks from a spreadsheet: download the class template, fill it in, upload it, see what it
    would change, then save. Nothing is saved before the teacher confirms.
    """
    from collections import Counter

    from core.exports import spreadsheet

    from ..mark_import import apply, check, headers_for, read_rows, template_rows

    data = request.POST if request.method == "POST" else request.GET
    schedule, section, students, existing = _paper_and_class(request, data)
    if schedule.exam.status == "published" and not active_unlock(request.user, schedule):
        raise PermissionDenied("Results are published. Request an unlock before importing marks.")
    parts = list(schedule.components.all())
    session_key = f"mark_import:{schedule.pk}:{section.pk}"
    grid = reverse("examinations:marks") + "?" + urlencode({"schedule": schedule.pk, "section": section.pk})

    fmt = request.GET.get("download")
    if fmt in ("xlsx", "csv"):
        return spreadsheet(
            f"marks-{schedule.subject.code or schedule.pk}-{section.name}",
            headers_for(schedule, parts),
            template_rows(parts, students, existing),
            fmt,
            preamble=[
                ("Paper", f"{schedule.exam.name}: {schedule.subject.name}"),
                ("Section", str(section)),
                ("Full marks", plain(schedule.full_marks)),
                ("How to fill it", "Write ABS for an absent student. A blank row leaves that mark as it is."),
            ],
        )

    context = {
        "schedule": schedule,
        "section": section,
        "parts": parts,
        "grid": grid,
        "students": len(students),
        "page_title": "Import marks",
    }
    if request.method == "POST" and request.POST.get("action") == "check":
        upload = request.FILES.get("file")
        request.session.pop(session_key, None)
        if upload is None:
            messages.error(request, "Choose the filled-in template to upload.")
        else:
            try:
                prepared, problems, skipped = check(schedule, parts, students, existing, read_rows(upload))
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                counts = Counter(entry["action"] for entry in prepared)
                if not problems and (counts["new"] or counts["changed"]):
                    request.session[session_key] = prepared
                context.update(
                    prepared=prepared,
                    problems=problems,
                    skipped=skipped,
                    counts=counts,
                    to_save=counts["new"] + counts["changed"],
                    filename=upload.name,
                )
    elif request.method == "POST" and request.POST.get("action") == "confirm":
        prepared = request.session.pop(session_key, None)
        if prepared is None:
            messages.error(request, "Nothing was waiting to be saved. Upload the file again.")
        else:
            try:
                saved = apply(
                    user=request.user, schedule=schedule, section=section, prepared=prepared, students=students
                )
            except MarkEntryError as exc:
                names = {e.pk: e.student.full_name for e in students}
                messages.error(
                    request,
                    "Nothing was saved. "
                    + "; ".join(f"{names.get(pk, pk)}: {message}" for pk, message in exc.errors.items()),
                )
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
            else:
                messages.success(request, f"Imported {len(saved)} change(s) from the file.")
                return redirect(grid)
    return render(request, "examinations/marks_import.html", context)


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
    from ..feedback import (
        COMMENT_LIMIT,
        CommentError,
        effort_label,
        is_class_teacher,
        save_overall_comments,
        save_subject_comments,
    )
    from ..models import ResultComment

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

        from ..models import GradeForecast

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

    from academics.models import AcademicYear

    from ..feedback import approve_forecasts, may_forecast, record_forecasts
    from ..models import GradeForecast
    from ..subjects import paper_role

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
