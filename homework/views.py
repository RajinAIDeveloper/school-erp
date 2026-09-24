"""
Homework screens: setting and checking work (staff), and the to-do list (students and families).
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time
from django.utils.translation import gettext
from django.views.decorators.http import require_POST

from core.forms import TailwindFormMixin

from . import services
from .access import (
    homework_view,
    may_manage,
    may_mark,
    may_view,
    settable_units,
    task_for_staff,
    teaching_pairs,
    visible_tasks,
)
from .family import family_students, family_submission
from .family import todo as todo_groups
from .files import prepare, serve, too_large
from .models import DailyLimit, Submission, SubmissionFile, Task, TaskResource


def _year(school):
    from academics.models import AcademicYear

    return AcademicYear.current_for(school)


def _errors(exc):
    return exc.messages if hasattr(exc, "messages") else [str(exc)]


class TaskForm(TailwindFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = [
            "title",
            "purpose",
            "instructions",
            "kind",
            "hand_in",
            "estimated_minutes",
            "marking",
            "max_marks",
            "marks_visible",
            "allow_late",
        ]
        widgets = {"instructions": forms.Textarea(attrs={"rows": 5})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("kind", "hand_in", "marking"):
            self.fields[name].choices = [(value, gettext(label)) for value, label in self.fields[name].choices]
        self.fields["estimated_minutes"].widget.attrs.update(min=1, max=300)
        self.fields["max_marks"].widget.attrs.update(min=1, step="0.5")


# ------------------------------------------------------------------ staff: the list


@homework_view("homework.view_task", also="own sections and subjects; a class teacher reads their section")
def task_list(request):
    now = timezone.now()
    tasks = (
        visible_tasks(request.user, request.school)
        .select_related("subject", "class_level", "teacher")
        .prefetch_related("targets__section")
        .annotate(
            expected=Count("submissions", distinct=True),
            done=Count("submissions", filter=Q(submissions__status__in=Submission.HANDED_IN), distinct=True),
            unchecked=Count(
                "submissions",
                filter=Q(submissions__status=Submission.Status.PENDING, submissions__target__due_at__lte=now),
                distinct=True,
            ),
            open_targets=Count("targets", filter=Q(targets__due_at__gt=now), distinct=True),
            to_return=Count(
                "submissions",
                filter=Q(submissions__source=Submission.Source.ONLINE)
                & (
                    Q(submissions__returned_at__isnull=True)
                    | Q(submissions__handed_in_at__gt=F("submissions__returned_at"))
                ),
                distinct=True,
            ),
        )
    )
    live = Q(status=Task.Status.PUBLISHED, publish_at__lte=now)
    tabs = {
        "upcoming": tasks.filter(live, open_targets__gt=0).order_by("targets__due_at"),
        "check": tasks.filter(live, Q(unchecked__gt=0, hand_in=Task.HandIn.IN_CLASS) | Q(to_return__gt=0)).order_by(
            "-publish_at"
        ),
        "scheduled": tasks.filter(status=Task.Status.PUBLISHED, publish_at__gt=now).order_by("publish_at"),
        "drafts": tasks.filter(status=Task.Status.DRAFT).order_by("-updated_at"),
        "past": tasks.filter(live, open_targets=0).order_by("-publish_at"),
        "withdrawn": tasks.filter(status=Task.Status.WITHDRAWN).order_by("-updated_at"),
    }
    tab = request.GET.get("tab") if request.GET.get("tab") in tabs else "upcoming"
    chosen = tabs[tab]
    subject = request.GET.get("subject", "")
    if subject.isdigit():
        chosen = chosen.filter(subject_id=int(subject))
    section = request.GET.get("section", "")
    if section.isdigit():
        chosen = chosen.filter(targets__section_id=int(section))
    page = Paginator(chosen.distinct(), 25).get_page(request.GET.get("page"))
    units = settable_units(request.user, request.school, _year(request.school))
    return render(
        request,
        "homework/list.html",
        {
            "page_obj": page,
            "tab": tab,
            "counts": {name: qs.distinct().count() for name, qs in tabs.items() if name in ("check", "drafts")},
            "can_set": bool(units) and request.user.has_perm("homework.add_task"),
            "subjects": sorted({subject for _level, subject, _s in units}, key=lambda s: s.name),
            "sections": sorted({s for _l, _s, found in units for s in found}, key=lambda s: str(s)),
            "chosen_subject": subject,
            "chosen_section": section,
            "now": now,
            "page_title": gettext("Homework"),
        },
    )


# ------------------------------------------------------------------ staff: setting work


def _read_targets(request, sections):
    """The sections ticked on the form, each with its due time; and any problems found."""
    targets, problems = {}, []
    for section in sections:
        if not request.POST.get(f"section_{section.pk}"):
            continue
        try:
            day = parse_date(request.POST.get(f"due_date_{section.pk}", ""))
            moment = parse_time(request.POST.get(f"due_time_{section.pk}", "") or "")
        except ValueError:
            day, moment = None, None
        if day is None or moment is None:
            problems.append(gettext("%(section)s: give a due date and time.") % {"section": section})
            continue
        targets[section] = timezone.make_aware(datetime.combine(day, moment))
    return targets, problems


def _publish_at(request):
    raw = request.POST.get("publish_at", "")
    try:
        value = parse_datetime(raw) if raw else None
    except ValueError:
        value = None
    if value is not None and timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def _form_rows(task, sections, targets, ticked, after, year, minutes, only_some):
    """One row per section of the class: ticked or not, its due time, why that time, and the load."""
    from holidays.models import is_holiday

    rows = []
    existing = {t.section_id: t.due_at for t in task.targets.all()} if task.pk else {}
    for section in sections:
        if section in targets:
            due, reason = targets[section], ""
        elif section.pk in existing:
            due, reason = existing[section.pk], ""
        else:
            due, reason = services.suggest_due(section, task.subject, after, year)
        chosen = section.pk in ticked
        local = timezone.localtime(due)
        rows.append(
            {
                "section": section,
                "chosen": chosen,
                "date": local.date(),
                "time": local.time(),
                "reason": reason,
                "closed": is_holiday(section.school, local.date()),
                "load": services.load_with(section, due, minutes, exclude=task.pk, whole_section=not only_some)
                if chosen
                else None,
            }
        )
    return rows


def _task_form(request, task, level, subject, sections, year, copy=None):
    now = timezone.now()
    eligible = services.eligible(year, level, subject, sections)
    if request.method == "POST":
        targets, problems = _read_targets(request, sections)
        ticked = {section.pk for section in sections if request.POST.get(f"section_{section.pk}")}
        only_some = bool(request.POST.get("only_some"))
        selected = [int(v) for v in request.POST.getlist("students") if v.isdigit()] if only_some else None
        action = request.POST.get("action", "")
        publish_at = _publish_at(request)
        raw_minutes = request.POST.get("estimated_minutes", "")
        minutes = int(raw_minutes) if raw_minutes.isdigit() else task.estimated_minutes
        resources, link, link_title = [], request.POST.get("resource_link", ""), request.POST.get("resource_title", "")
        removed = [int(v) for v in request.POST.getlist("remove_resource") if v.isdigit()]
        if too_large(request):
            problems.append(gettext("The files are larger than the upload limit of 30 MB."))
        elif action != "check":
            for upload in request.FILES.getlist("resource_files"):
                try:
                    resources.append((prepare(upload), upload.name))
                except ValidationError as exc:
                    problems.extend(_errors(exc))
            try:
                link = services.clean_link(link)
            except ValidationError as exc:
                problems.extend(_errors(exc))
        if action == "check":
            # Only looking at the load: show the form as typed, without complaining yet.
            posted = {name: request.POST.get(name, "") for name in TaskForm.Meta.fields}
            posted["marks_visible"] = bool(request.POST.get("marks_visible"))
            form = TaskForm(instance=task, initial=posted)
            problems = []
        else:
            form = TaskForm(request.POST, instance=task)
        if action in ("draft", "publish", "schedule", "save") and form.is_valid() and not problems:
            try:
                saved = services.save_task(
                    user=request.user,
                    task=form.instance,
                    targets=targets,
                    action=action,
                    selected=selected,
                    publish_at=publish_at,
                    now=now,
                )
            except ValidationError as exc:
                problems.extend(_errors(exc))
            else:
                services.add_resources(user=request.user, task=saved, prepared=resources, link=link, title=link_title)
                if removed:
                    services.remove_resources(user=request.user, task=saved, ids=removed)
                messages.success(
                    request,
                    {
                        "draft": gettext("Draft saved. Only you and the school's managers can see it."),
                        "publish": gettext("Homework set."),
                        "schedule": gettext("Homework scheduled."),
                        "save": gettext("Changes saved."),
                    }[action],
                )
                for target in saved.targets.select_related("section__class_level"):
                    load = services.load_with(target.section, target.due_at, 0)
                    if load["over"]:
                        day = next(d for d in load["days"] if d["this"])
                        messages.warning(
                            request,
                            gettext(
                                "%(section)s now has %(minutes)s minutes of homework due on %(day)s; the "
                                "school's limit for the class is %(limit)s."
                            )
                            % {
                                "section": target.section,
                                "minutes": day["minutes"],
                                "day": timezone.localdate(target.due_at).strftime("%d %b"),
                                "limit": day["limit"],
                            },
                        )
                return redirect("homework:detail", pk=saved.pk)
        for problem in problems:
            form.add_error(None, problem)
        after = publish_at if publish_at and publish_at > now else now
    else:
        initial = {}
        if copy is not None:
            initial = {name: getattr(copy, name) for name in TaskForm.Meta.fields}
        form = TaskForm(instance=task, initial=initial)
        targets, only_some = {}, task.selected_only
        existing = {t.section_id for t in task.targets.all()} if task.pk else set()
        ticked = existing or {section.pk for section in sections}
        selected = list(task.submissions.values_list("enrollment_id", flat=True)) if task.pk and only_some else None
        minutes = task.estimated_minutes if task.pk else initial.get("estimated_minutes", task.estimated_minutes)
        after = task.publish_at if task.pk and task.publish_at and task.publish_at > now else now
    rows = _form_rows(task, sections, targets, ticked, after, year, minutes or 0, only_some)
    return render(
        request,
        "homework/form.html",
        {
            "form": form,
            "task": task,
            "level": level,
            "subject": subject,
            "rows": rows,
            "eligible": eligible,
            "resources": list(task.resources.all()) if task.pk else [],
            "only_some": only_some,
            "selected": set(selected or []),
            "copy": copy,
            "live": task.pk and task.is_live(now),
            "publish_at": request.POST.get("publish_at", "") if request.method == "POST" else "",
            "page_title": gettext("Edit homework") if task.pk else gettext("Set homework"),
        },
    )


@homework_view("homework.add_task", also="own sections and subjects")
def task_create(request):
    year = _year(request.school)
    if year is None:
        messages.error(request, gettext("Set up the current academic year first."))
        return redirect("homework:list")
    units = settable_units(request.user, request.school, year)
    copy = None
    if request.GET.get("copy", "").isdigit():
        copy = task_for_staff(request.user, request.school, int(request.GET["copy"]))
    key = request.GET.get("unit") or (f"{copy.class_level_id}-{copy.subject_id}" if copy else "")
    unit = next(((lv, sj, found) for lv, sj, found in units if f"{lv.pk}-{sj.pk}" == key), None)
    if unit is None:
        if key:
            raise Http404
        return render(request, "homework/choose.html", {"units": units, "page_title": gettext("Set homework")})
    level, subject, sections = unit
    task = Task(school=request.school, academic_year=year, class_level=level, subject=subject)
    if copy is not None:
        task.copied_from = copy
    return _task_form(request, task, level, subject, sections, year, copy)


@homework_view("homework.change_task", also="the teacher of every section it is set for, or a manager")
def task_edit(request, pk):
    task = task_for_staff(request.user, request.school, pk)
    if not may_manage(request.user, task) or task.status == Task.Status.WITHDRAWN:
        raise PermissionDenied
    units = settable_units(request.user, request.school, task.academic_year)
    sections = next((found for lv, sj, found in units if lv.pk == task.class_level_id and sj.pk == task.subject_id), [])
    # Keep sections already set even if the teacher no longer takes them, so nothing is lost.
    extra = [t.section for t in task.targets.select_related("section") if t.section not in sections]
    return _task_form(
        request,
        task,
        task.class_level,
        task.subject,
        sorted([*sections, *extra], key=lambda s: s.name),
        task.academic_year,
    )


# ------------------------------------------------------------------ staff: one task


def _counts(rows, now):
    counts = {
        "expected": 0,
        "done": 0,
        "partial": 0,
        "not_done": 0,
        "absent": 0,
        "excused": 0,
        "missing": 0,
        "pending": 0,
        "family": 0,
    }
    for row in rows:
        counts[row.status if row.status != Submission.Status.PENDING else "pending"] += 1
        if row.status not in Submission.NOT_EXPECTED:
            counts["expected"] += 1
        if row.is_missing(now):
            counts["missing"] += 1
        if row.source == Submission.Source.FAMILY:
            counts["family"] += 1
    return counts


@homework_view("homework.view_task", also="own sections and subjects; a class teacher reads their section")
def task_detail(request, pk):
    now = timezone.now()
    task = task_for_staff(request.user, request.school, pk)
    services.sync_task(task, now)
    pairs = teaching_pairs(request.user, request.school, task.academic_year)
    targets = []
    for target in task.targets.select_related("section__class_level"):
        if not may_view(request.user, task, target.section, pairs):
            continue
        rows = list(target.submissions.all())
        targets.append(
            {
                "target": target,
                "counts": _counts(rows, now),
                "can_check": may_mark(request.user, task, target.section, pairs),
                "due": target.due_at <= now,
            }
        )
    return render(
        request,
        "homework/detail.html",
        {
            "task": task,
            "targets": targets,
            "can_manage": may_manage(request.user, task, pairs),
            "answered": services.has_responses(task),
            "resources": list(task.resources.all()),
            "live": task.is_live(now),
            "scheduled": task.is_scheduled(now),
            "page_title": task.title,
        },
    )


@homework_view("homework.change_task", also="the teacher of every section it is set for, or a manager")
@require_POST
def task_withdraw(request, pk):
    task = task_for_staff(request.user, request.school, pk)
    try:
        services.withdraw(user=request.user, task=task)
    except ValidationError as exc:
        messages.error(request, " ".join(_errors(exc)))
    else:
        messages.success(request, gettext("Homework withdrawn. Families no longer see it."))
    return redirect("homework:detail", pk=task.pk)


@homework_view("homework.delete_task", also="the teacher of every section it is set for, or a manager")
@require_POST
def task_delete(request, pk):
    task = task_for_staff(request.user, request.school, pk)
    try:
        services.delete_task(user=request.user, task=task)
    except ValidationError as exc:
        messages.error(request, " ".join(_errors(exc)))
        return redirect("homework:detail", pk=task.pk)
    messages.success(request, gettext("Homework deleted."))
    return redirect("homework:list")


# ------------------------------------------------------------------ staff: checking work


def _decimal(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValidationError(gettext("Marks must be numbers.")) from None


def _draft_base(rows):
    import hashlib

    state = ",".join(f"{row.pk}:{row.version}" for row in rows)
    return hashlib.sha256(state.encode()).hexdigest()[:16]


@homework_view("homework.view_submission", also="teachers of the subject there; the class teacher reads it")
def check(request, pk, section):
    from attendance.models import StudentAttendance
    from students.models import Enrollment

    now = timezone.now()
    task = task_for_staff(request.user, request.school, pk)
    target = get_object_or_404(task.targets.select_related("section__class_level"), section_id=section)
    pairs = teaching_pairs(request.user, request.school, task.academic_year)
    if not may_view(request.user, task, target.section, pairs):
        raise PermissionDenied
    editable = may_mark(request.user, task, target.section, pairs) and request.user.has_perm(
        "homework.change_submission"
    )
    services.sync_task(task, now)
    if request.method == "POST":
        if not editable:
            raise PermissionDenied
        given, problems = [], []
        rows = {row.pk: row for row in target.submissions.all()}
        for raw in request.POST.getlist("row"):
            if not raw.isdigit() or int(raw) not in rows:
                raise PermissionDenied
            row = rows[int(raw)]
            try:
                mark = _decimal(request.POST.get(f"mark_{row.pk}"))
            except ValidationError as exc:
                problems.extend(_errors(exc))
                continue
            version = request.POST.get(f"version_{row.pk}", "")
            given.append(
                {
                    "id": row.pk,
                    "version": int(version) if version.isdigit() else -1,
                    "status": request.POST.get(f"status_{row.pk}", Submission.Status.PENDING),
                    "late": bool(request.POST.get(f"late_{row.pk}")),
                    "mark": mark,
                    "grade": request.POST.get(f"grade_{row.pk}", ""),
                    "feedback": request.POST.get(f"feedback_{row.pk}", ""),
                    "reason": request.POST.get(f"reason_{row.pk}", row.reason),
                }
            )
        if not problems:
            try:
                changed = services.check_rows(
                    user=request.user,
                    task=task,
                    target=target,
                    rows=given,
                    return_work=bool(request.POST.get("return_work")),
                    now=now,
                )
            except ValidationError as exc:
                problems.extend(_errors(exc))
            else:
                messages.success(request, gettext("Saved: %(n)s record(s) changed.") % {"n": changed})
                return redirect("homework:check", pk=task.pk, section=target.section_id)
        for problem in problems:
            messages.error(request, problem)
    rows = list(
        target.submissions.select_related("enrollment__student", "enrollment__section", "handed_in_by")
        .annotate(pages=Count("files"))
        .order_by("enrollment__roll_number")
    )
    absent = set(
        StudentAttendance.objects.filter(
            enrollment__in=[row.enrollment_id for row in rows], date=target.due_on, status="absent"
        ).values_list("enrollment_id", flat=True)
    )
    for row in rows:
        # Absent from school on the day it was due: offered as Absent until the teacher says otherwise.
        row.shown_status = (
            Submission.Status.ABSENT
            if row.status == Submission.Status.PENDING and row.enrollment_id in absent and editable
            else row.status
        )
        row.left = row.enrollment.status != Enrollment.Status.ENROLLED or row.enrollment.section_id != target.section_id
    choices = [(value, gettext(label)) for value, label in Submission.Status.choices]
    return render(
        request,
        "homework/check.html",
        {
            "task": task,
            "target": target,
            "rows": rows,
            "choices": choices,
            "editable": editable and task.status == Task.Status.PUBLISHED,
            "due": target.due_at <= now,
            "counts": _counts(rows, now),
            "draft_base": _draft_base(rows),
            "page_title": f"{task.title} · {target.section}",
        },
    )


# ------------------------------------------------------------------ settings: daily limits


@homework_view("homework.change_dailylimit", also="the school's managers")
def limits(request):
    from academics.models import ClassLevel

    levels = list(ClassLevel.objects.filter(school=request.school, is_active=True).order_by("order"))
    current = {row.class_level_id: row for row in DailyLimit.objects.filter(school=request.school)}
    if request.method == "POST":
        problems = []
        wanted = {}
        raw_months = (request.POST.get("files_months") or "").strip()
        if not raw_months.isdigit() or not 1 <= int(raw_months) <= 60:
            problems.append(gettext("Keep handed-in files for 1 to 60 months."))
        for level in levels:
            raw = (request.POST.get(f"minutes_{level.pk}") or "").strip()
            if not raw:
                wanted[level] = None
            elif raw.isdigit() and 1 <= int(raw) <= 600:
                wanted[level] = int(raw)
            else:
                problems.append(gettext("%(level)s: give minutes from 1 to 600, or leave it blank.") % {"level": level})
        if problems:
            for problem in problems:
                messages.error(request, problem)
        else:
            for level, minutes in wanted.items():
                row = current.get(level.pk)
                if minutes is None and row is not None:
                    row.delete()
                elif minutes is not None and row is None:
                    DailyLimit.objects.create(school=request.school, class_level=level, minutes=minutes)
                elif minutes is not None and row.minutes != minutes:
                    row.minutes = minutes
                    row.save(update_fields=["minutes", "updated_at"])
            if int(raw_months) != request.school.homework_files_months:
                request.school.homework_files_months = int(raw_months)
                request.school.save(update_fields=["homework_files_months", "updated_at"])
            messages.success(request, gettext("Homework settings saved."))
            return redirect("homework:limits")
    return render(
        request,
        "homework/limits.html",
        {
            "levels": [(level, current.get(level.pk)) for level in levels],
            "files_months": request.school.homework_files_months,
            "page_title": gettext("Homework settings"),
        },
    )


# ------------------------------------------------------------------ students and families


def _family_student(request):
    """The child whose homework to show: the student themself, or one of the guardian's children."""
    students = list(family_students(request.user, request.school).order_by("student_id"))
    if not students:
        raise PermissionDenied("This account has no linked student or guardian profile.")
    raw = request.GET.get("student")
    if raw:
        student = next((s for s in students if str(s.pk) == raw), None)
        if student is None:
            raise Http404
        return students, student
    return students, students[0]


@homework_view(None, also="own children only")
def todo(request):
    students, student = _family_student(request)
    enrollment = student.current_enrollment
    groups = todo_groups(enrollment) if enrollment else None
    return render(
        request,
        "homework/todo.html",
        {
            "students": students,
            "selected": student,
            "enrollment": enrollment,
            "groups": groups,
            "now": timezone.now(),
            "page_title": gettext("Homework"),
        },
    )


@homework_view(None, also="own children only")
def todo_task(request, pk):
    from django.http import JsonResponse

    now = timezone.now()
    row = family_submission(request.user, request.school, pk, now)
    if request.method == "POST":
        fetched = request.headers.get("X-Requested-With") == "fetch"
        action = request.POST.get("action")
        try:
            if too_large(request):
                raise ValidationError(gettext("The files are larger than the upload limit of 30 MB."))
            if action == "hand_in":
                services.hand_in_online(
                    user=request.user,
                    submission=row,
                    uploads=request.FILES.getlist("pages"),
                    note=request.POST.get("note", ""),
                    now=now,
                )
                message = gettext("Handed in. The teacher can see it now.")
            elif action == "remove":
                page = get_object_or_404(row.files, pk=request.POST.get("page") or 0)
                services.remove_page(user=request.user, page=page, now=now)
                message = gettext("Page taken out.")
            else:
                services.family_mark_done(user=request.user, submission=row, done=action == "done", now=now)
                message = ""
        except ValidationError as exc:
            if fetched:
                return JsonResponse({"ok": False, "errors": _errors(exc)}, status=400)
            messages.error(request, " ".join(_errors(exc)))
        else:
            if message and not fetched:
                messages.success(request, message)
            if fetched:
                return JsonResponse({"ok": True, "message": message})
        return redirect(reverse("homework:todo_task", args=[row.pk]))
    task = row.task
    returned = row.returned_at is not None
    return render(
        request,
        "homework/todo_task.html",
        {
            "row": row,
            "task": task,
            "overdue": row.is_missing(now),
            "returned": returned,
            "show_mark": returned and task.marks_visible,
            "can_tick": task.hand_in != Task.HandIn.ONLINE and row.checked_at is None and task.is_live(now),
            "online": task.hand_in == Task.HandIn.ONLINE,
            "closed": services.hand_in_closed(row, now) if task.hand_in == Task.HandIn.ONLINE else None,
            "pages": list(row.files.all()),
            "resources": list(task.resources.all()),
            "is_guardian": row.enrollment.student.user_id != request.user.pk,
            "page_title": task.title,
        },
    )


# ------------------------------------------------------------------ one student's work


def _moment(request, prefix):
    try:
        day = parse_date(request.POST.get(f"{prefix}_date", "") or "")
        moment = parse_time(request.POST.get(f"{prefix}_time", "") or "")
    except ValueError:
        return None
    if day is None:
        return None
    return timezone.make_aware(datetime.combine(day, moment or datetime.min.time().replace(hour=23, minute=59)))


@homework_view("homework.view_submission", also="teachers of the subject there; the class teacher reads it")
def review(request, pk):
    now = timezone.now()
    row = get_object_or_404(
        Submission.objects.select_related("task__subject", "target__section", "enrollment__student", "handed_in_by"),
        school=request.school,
        pk=pk,
    )
    task = task_for_staff(request.user, request.school, row.task_id)
    pairs = teaching_pairs(request.user, request.school, task.academic_year)
    if not may_view(request.user, task, row.target.section, pairs):
        raise PermissionDenied
    editable = (
        may_mark(request.user, task, row.target.section, pairs)
        and request.user.has_perm("homework.change_submission")
        and task.status == Task.Status.PUBLISHED
    )
    if request.method == "POST":
        if not editable:
            raise PermissionDenied
        action = request.POST.get("action")
        try:
            if action == "redo":
                services.ask_to_redo(
                    user=request.user,
                    submission=row,
                    feedback=request.POST.get("feedback", ""),
                    extend_to=_moment(request, "until"),
                    now=now,
                )
                messages.success(request, gettext("Sent back to be done again."))
            elif action == "extend":
                services.give_more_time(user=request.user, submission=row, extend_to=_moment(request, "until"), now=now)
                messages.success(request, gettext("More time given."))
            elif action == "excuse":
                services.excuse(user=request.user, submission=row, reason=request.POST.get("reason", ""), now=now)
                messages.success(request, gettext("Excused."))
            else:
                version = request.POST.get("version", "")
                services.check_rows(
                    user=request.user,
                    task=task,
                    target=row.target,
                    rows=[
                        {
                            "id": row.pk,
                            "version": int(version) if version.isdigit() else -1,
                            "status": request.POST.get("status", row.status),
                            "late": bool(request.POST.get("late")),
                            "mark": _decimal(request.POST.get("mark")),
                            "grade": request.POST.get("grade", ""),
                            "feedback": request.POST.get("feedback", ""),
                            "reason": row.reason,
                        }
                    ],
                    return_work=bool(request.POST.get("return_work")),
                    now=now,
                )
                messages.success(request, gettext("Saved."))
                following = (
                    row.target.submissions.filter(
                        source=Submission.Source.ONLINE, enrollment__roll_number__gt=row.enrollment.roll_number
                    )
                    .filter(Q(returned_at__isnull=True) | Q(handed_in_at__gt=F("returned_at")))
                    .order_by("enrollment__roll_number")
                    .first()
                )
                if following is not None and request.POST.get("next"):
                    return redirect("homework:review", pk=following.pk)
        except ValidationError as exc:
            for problem in _errors(exc):
                messages.error(request, problem)
        return redirect("homework:review", pk=row.pk)
    pages = list(row.files.all())
    attempts = sorted({page.attempt for page in pages}, reverse=True)
    return render(
        request,
        "homework/review.html",
        {
            "row": row,
            "task": task,
            "editable": editable,
            "attempts": [(n, [page for page in pages if page.attempt == n]) for n in attempts],
            "choices": [(value, gettext(label)) for value, label in Submission.Status.choices],
            "returned": services.is_returned(row),
            "overdue": row.is_missing(now),
            "page_title": f"{row.enrollment.student.full_name} · {task.title}",
        },
    )


# ------------------------------------------------------------------ files, checked every time


def _family_may_open(user, school, row, now):
    return row.task.is_live(now) and family_students(user, school).filter(pk=row.enrollment.student_id).exists()


@homework_view(None, also="the student, their family, the subject's teachers, the class teacher, managers")
def hand_in_file(request, pk):
    """A page of a student's hand-in, for the people who may see that student's work."""
    now = timezone.now()
    page = get_object_or_404(
        SubmissionFile.objects.select_related(
            "submission__task__subject", "submission__target__section", "submission__enrollment__student"
        ),
        school=request.school,
        pk=pk,
    )
    row = page.submission
    allowed = _family_may_open(request.user, request.school, row, now)
    if not allowed and request.user.has_perm("homework.view_submission"):
        task = visible_tasks(request.user, request.school).filter(pk=row.task_id).first()
        allowed = task is not None and may_view(request.user, task, row.target.section)
    if not allowed:
        raise Http404
    ext = {"pdf": ".pdf", "jpeg": ".jpg", "docx": ".docx"}.get(page.kind, "")
    name = f"{row.task.subject.code or 'homework'}-{row.task_id}-{row.enrollment.student.student_id}-p{page.page}{ext}"
    return serve(page.file, page.kind, name)


@homework_view(None, also="the families the task is set for, and staff who may open the task")
def resource_file(request, pk):
    """A worksheet given with a task."""
    now = timezone.now()
    resource = get_object_or_404(TaskResource.objects.select_related("task"), school=request.school, pk=pk)
    if not resource.file:
        raise Http404
    task = resource.task
    allowed = (
        task.is_live(now)
        and task.submissions.filter(enrollment__student__in=family_students(request.user, request.school)).exists()
    )
    if not allowed and request.user.has_perm("homework.view_task"):
        allowed = visible_tasks(request.user, request.school).filter(pk=task.pk).exists()
    if not allowed:
        raise Http404
    ext = {"pdf": ".pdf", "jpeg": ".jpg", "docx": ".docx"}.get(resource.kind, "")
    return serve(resource.file, resource.kind, f"{resource.title[:60]}{ext}")
