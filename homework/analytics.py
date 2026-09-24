"""
Homework analytics: how much work students hand in, on time, and how well they do, for each
student, subject, section, class, teacher and the whole school.

The figures count only work whose outcome is known. A student who was absent or excused is
left out; work checked in class that nobody has checked yet is unknown, and is counted apart
rather than as missing. Marks are averaged as percentages of each task's marks, over work that
has been returned.

Everything goes through who may see what: a family sees their own child against the section as
a whole (never another child's name); a subject teacher, the sections and subjects they teach;
a class teacher, their section; the school's managers, the school.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .access import teaching_pairs
from .followup import MISSING, UNKNOWN, rows_due
from .models import Submission, Task, TaskSection

PERIODS = ("4w", "term", "year")


def _midnight(day):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()))


def period_bounds(school, academic_year, period, now=None):
    """(start, end, period) of the last four weeks, the current term, or the year so far."""
    from academics.models import Term

    now = now or timezone.now()
    today = timezone.localdate(now)
    if period == "year" and academic_year is not None:
        return _midnight(academic_year.start_date), now, "year"
    if period == "term" and academic_year is not None:
        term = Term.objects.filter(academic_year=academic_year, start_date__lte=today, end_date__gte=today).first()
        if term is not None:
            return _midnight(term.start_date), now, "term"
    return now - timedelta(weeks=4), now, "4w"


def visible(user, school, scope, academic_year):
    """The homework records this viewer may count, as a filter."""
    if scope.whole_school:
        return Q()
    allowed = Q(pk__in=[])
    if scope.students:
        allowed |= Q(enrollment__student_id__in=scope.students)
    if scope.sections:
        allowed |= Q(target__section_id__in=scope.sections)
    for section_id, subject_id in teaching_pairs(user, school, academic_year):
        allowed |= Q(target__section_id=section_id, task__subject_id=subject_id)
    return allowed


def base_rows(school, academic_year, start, end):
    """Published, not withdrawn, due in the period, for students still on the roll."""
    from students.models import Enrollment, Student

    return rows_due(
        Submission.objects.filter(
            school=school,
            task__academic_year=academic_year,
            task__status=Task.Status.PUBLISHED,
            enrollment__status=Enrollment.Status.ENROLLED,
            enrollment__student__status=Student.Status.ACTIVE,
        ),
        start,
        end,
    )


MARK_PERCENT = ExpressionWrapper(
    F("mark") * Decimal(100) / F("task__max_marks"), output_field=DecimalField(max_digits=9, decimal_places=4)
)


def summarise(rows, *fields):
    """Figures for all the rows, or for each group of `fields`: known, handed in, on time, marks."""
    known = rows.exclude(status__in=Submission.NOT_EXPECTED).exclude(UNKNOWN)
    measures = {
        "known": Count("id"),
        "handed": Count("id", filter=Q(status__in=Submission.HANDED_IN)),
        "on_time": Count("id", filter=Q(status__in=Submission.HANDED_IN, late=False)),
        "family": Count("id", filter=Q(source=Submission.Source.FAMILY)),
        "mark": Avg(
            MARK_PERCENT, filter=Q(mark__isnull=False, task__marking=Task.Marking.MARKS, returned_at__isnull=False)
        ),
    }
    unchecked = rows.filter(UNKNOWN)
    if not fields:
        figures = known.aggregate(**measures)
        figures["unchecked"] = unchecked.count()
        return _rates(figures)
    groups = {tuple(row[f] for f in fields): row for row in known.values(*fields).annotate(**measures)}
    waiting = {tuple(row[f] for f in fields): row["n"] for row in unchecked.values(*fields).annotate(n=Count("id"))}
    result = []
    for key in set(groups) | set(waiting):
        row = {**dict(zip(fields, key, strict=True)), **groups.get(key, {}), "unchecked": waiting.get(key, 0)}
        result.append(_rates(row))
    return result


def _rates(row):
    known = row.get("known") or 0
    handed = row.get("handed") or 0
    row["known"], row["handed"] = known, handed
    row["missing"] = known - handed
    row["rate"] = round(100 * handed / known) if known else None
    row["on_time_rate"] = round(100 * (row.get("on_time") or 0) / known) if known else None
    row["family_share"] = round(100 * (row.get("family") or 0) / handed) if handed else None
    mark = row.get("mark")
    row["mark"] = Decimal(mark).quantize(Decimal("0.1")) if mark is not None else None
    row.setdefault("unchecked", 0)
    return row


def _by(rows, field):
    return {row[field]: row for row in summarise(rows, field)}


# ------------------------------------------------------------------ one student


def student_homework(student, user, school, scope, *, period="4w", now=None):
    """
    One student's homework over a period, as far as the viewer may see: the overall figures,
    each subject against the section's figures for it, month by month, and what is missing now.
    None when there is no homework to show.
    """
    now = now or timezone.now()
    enrollment = student.current_enrollment
    if enrollment is None:
        return None
    year = enrollment.academic_year
    start, end, period = period_bounds(school, year, period, now)
    allowed = visible(user, school, scope, year)
    everyone = base_rows(school, year, start, end).filter(allowed)
    mine = everyone.filter(enrollment=enrollment)
    if not mine.exists():
        return None
    subjects = _by(mine, "task__subject__name")
    # The section as a whole, in the subjects shown: a figure for the section, never a name.
    section = _by(
        base_rows(school, year, start, end).filter(
            target__section_id=enrollment.section_id, task__subject__name__in=list(subjects)
        ),
        "task__subject__name",
    )
    year_start, _end, _period = period_bounds(school, year, "year", now)
    missing_now = list(
        rows_due(Submission.objects.filter(enrollment=enrollment, task__status=Task.Status.PUBLISHED), year_start, now)
        .filter(allowed)
        .filter(MISSING)
        .select_related("task__subject")
        .order_by("-due")[:10]
    )
    return {
        "period": period,
        "overall": summarise(mine),
        "subjects": [
            {**subjects[name], "subject": name, "section_rate": (section.get(name) or {}).get("rate")}
            for name in sorted(subjects)
        ],
        "months": sorted(
            summarise(mine.annotate(month=TruncMonth("target__due_on")), "month"), key=lambda row: row["month"]
        ),
        "missing": missing_now,
    }


# ------------------------------------------------------------------ a subject in a section


def subject_grid(section, subject, academic_year, start, end):
    """Students against the tasks of one subject in one section, with each one's figures."""
    rows = base_rows(section.school, academic_year, start, end).filter(target__section=section, task__subject=subject)
    detail = list(rows.select_related("task", "enrollment__student").order_by("due", "task_id"))
    tasks, cells, students = [], {}, {}
    for row in detail:
        if row.task not in tasks:
            tasks.append(row.task)
        cells[(row.enrollment_id, row.task_id)] = row
        students.setdefault(row.enrollment_id, row.enrollment)
    per_student = _by(rows, "enrollment_id")
    per_task = _by(rows, "task_id")
    return {
        "tasks": [(task, per_task.get(task.pk)) for task in tasks],
        "students": [
            (enrollment, [cells.get((enrollment.pk, task.pk)) for task in tasks], per_student.get(enrollment.pk))
            for enrollment in sorted(students.values(), key=lambda e: e.roll_number)
        ],
        "overall": summarise(rows),
    }


# ------------------------------------------------------------------ a section across subjects


def section_grid(section, academic_year, start, end, allowed=None):
    """Students against subjects for a class teacher: each student's figures in each subject."""
    rows = base_rows(section.school, academic_year, start, end).filter(target__section=section)
    if allowed is not None:
        rows = rows.filter(allowed)
    cells = {
        (row["enrollment_id"], row["task__subject__name"]): row
        for row in summarise(rows, "enrollment_id", "task__subject__name")
    }
    subjects = sorted({name for _e, name in cells})
    per_student = _by(rows, "enrollment_id")
    per_subject = _by(rows, "task__subject__name")
    from students.models import Enrollment

    enrollments = Enrollment.objects.filter(pk__in=per_student).select_related("student").order_by("roll_number")
    return {
        "subjects": [(name, per_subject.get(name)) for name in subjects],
        "students": [
            (enrollment, [cells.get((enrollment.pk, name)) for name in subjects], per_student.get(enrollment.pk))
            for enrollment in enrollments
        ],
        "overall": summarise(rows),
    }


# ------------------------------------------------------------------ the whole school


def school_overview(school, academic_year, start, end):
    """
    The school's homework over a period: by class, section, subject and teacher, month by month,
    how promptly teachers check work, and how much is due each school day against the limits.
    """
    from academics.models import ClassLevel
    from employees.models import Employee
    from holidays.services import working_days

    from .services import limit_for

    rows = base_rows(school, academic_year, start, end)
    classes = sorted(
        summarise(rows, "target__section__class_level_id", "target__section__class_level__name"),
        key=lambda row: row["target__section__class_level_id"],
    )
    levels = {level.pk: level for level in ClassLevel.objects.filter(school=school)}
    for row in classes:
        row["order"] = levels[row["target__section__class_level_id"]].order
    classes.sort(key=lambda row: row["order"])
    sections = sorted(
        summarise(rows, "target__section_id", "target__section__name", "target__section__class_level__name"),
        key=lambda row: (row["target__section__class_level__name"], row["target__section__name"]),
    )
    subjects = sorted(summarise(rows, "task__subject__name"), key=lambda row: row["task__subject__name"])

    teachers = {row["task__teacher_id"]: row for row in summarise(rows, "task__teacher_id")}
    set_counts = dict(
        Task.objects.filter(
            school=school, academic_year=academic_year, status=Task.Status.PUBLISHED, publish_at__range=(start, end)
        )
        .values_list("teacher_id")
        .annotate(n=Count("id"))
    )
    checked = {
        row["task__teacher_id"]: row
        for row in rows.filter(checked_at__isnull=False)
        .values("task__teacher_id")
        .annotate(
            checked=Count("id"),
            prompt=Count("id", filter=Q(checked_at__lte=F("target__due_at") + timedelta(days=7))),
        )
    }
    names = {e.pk: str(e) for e in Employee.objects.filter(pk__in=set(teachers) | set(set_counts))}
    by_teacher = []
    for teacher_id in set(teachers) | set(set_counts):
        figures = teachers.get(teacher_id) or _rates({"task__teacher_id": teacher_id})
        prompt = checked.get(teacher_id)
        by_teacher.append(
            {
                **figures,
                "teacher": names.get(teacher_id, "—"),
                "tasks": set_counts.get(teacher_id, 0),
                "prompt_rate": round(100 * prompt["prompt"] / prompt["checked"])
                if prompt and prompt["checked"]
                else None,
            }
        )
    by_teacher.sort(key=lambda row: row["teacher"])

    # How much is due on each school day, against each class's limit.
    first, last = timezone.localdate(start), timezone.localdate(end)
    school_days = working_days(school, first, last)
    loads = list(
        TaskSection.objects.filter(
            school=school,
            task__academic_year=academic_year,
            task__status=Task.Status.PUBLISHED,
            task__selected_only=False,
            due_on__range=(first, last),
        )
        .values("section_id", "section__class_level_id", "due_on")
        .annotate(minutes=Sum("task__estimated_minutes"))
    )
    load_by_class = {}
    for row in loads:
        entry = load_by_class.setdefault(
            row["section__class_level_id"], {"minutes": 0, "sections": set(), "over": 0, "heaviest": 0}
        )
        entry["minutes"] += row["minutes"]
        entry["sections"].add(row["section_id"])
        entry["heaviest"] = max(entry["heaviest"], row["minutes"])
    for level_id, entry in load_by_class.items():
        limit = limit_for(levels[level_id])
        entry["limit"] = limit
        entry["over"] = sum(
            1 for row in loads if row["section__class_level_id"] == level_id and limit and row["minutes"] > limit
        )
        sections_count = levels[level_id].sections.filter(is_active=True).count() or 1
        entry["average"] = round(entry["minutes"] / (sections_count * school_days)) if school_days else None
    load = [
        {"level": levels[level_id], **entry}
        for level_id, entry in sorted(load_by_class.items(), key=lambda item: levels[item[0]].order)
    ]

    return {
        "overall": summarise(rows),
        "classes": classes,
        "sections": sections,
        "subjects": subjects,
        "teachers": by_teacher,
        "months": sorted(
            summarise(rows.annotate(month=TruncMonth("target__due_on")), "month"), key=lambda row: row["month"]
        ),
        "load": load,
        "school_days": school_days,
    }
