"""
Following homework up: the week ahead for a class, the work a section has not handed in, and
homework marks taken into an exam part on purpose.

What counts as missing is the same everywhere, the weekly SMS included: work the teacher has
recorded as not done, or work handed in online that has not arrived by its due time. Work
checked in class that nobody has checked yet is not missing, only unknown.
"""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import Submission, Task

MISSING = Q(status=Submission.Status.NOT_DONE) | Q(status=Submission.Status.PENDING, task__hand_in=Task.HandIn.ONLINE)
UNKNOWN = Q(status=Submission.Status.PENDING) & ~Q(task__hand_in=Task.HandIn.ONLINE)


def rows_due(queryset, since, until):
    """Records whose own due time (an extension counts) falls in the period."""
    return queryset.annotate(due=Coalesce(F("extended_to"), F("target__due_at"))).filter(due__gte=since, due__lt=until)


def missing_work(section, weeks=4, now=None):
    """
    Every current student of a section with what they did and did not hand in over the last
    few weeks, most missing first.
    """
    from students.models import Enrollment, Student

    now = now or timezone.now()
    rows = rows_due(
        Submission.objects.filter(
            target__section=section,
            task__status=Task.Status.PUBLISHED,
            enrollment__status=Enrollment.Status.ENROLLED,
            enrollment__student__status=Student.Status.ACTIVE,
        ),
        now - timedelta(weeks=weeks),
        now,
    ).select_related("task__subject", "enrollment__student")
    students = {}
    for row in rows:
        entry = students.setdefault(
            row.enrollment_id,
            {"enrollment": row.enrollment, "known": 0, "handed": 0, "unchecked": 0, "missing": []},
        )
        if row.status in Submission.NOT_EXPECTED:
            continue
        if row.status == Submission.Status.PENDING and row.task.hand_in != Task.HandIn.ONLINE:
            entry["unchecked"] += 1
            continue
        entry["known"] += 1
        if row.status in Submission.HANDED_IN:
            entry["handed"] += 1
        else:
            entry["missing"].append(row)
    result = list(students.values())
    for entry in result:
        entry["rate"] = round(100 * entry["handed"] / entry["known"]) if entry["known"] else None
        entry["missing"].sort(key=lambda row: row.due)
    result.sort(key=lambda entry: (-len(entry["missing"]), entry["enrollment"].roll_number))
    return result


# ------------------------------------------------------------------ marks into an exam part


def export_marks(tasks, section, *, missing_as_zero, out_of, students, now=None):
    """
    One mark per student: the average of their percentages across the tasks, scaled to
    `out_of`. Excused and absent work is left out; missing work counts as nothing or is left
    out, as the teacher chooses. A student with nothing to count gets a blank.
    """
    now = now or timezone.now()
    percents = {}
    rows = Submission.objects.filter(task__in=tasks, target__section=section).select_related("task", "target")
    for row in rows:
        if row.status in Submission.NOT_EXPECTED:
            continue
        if row.mark is not None:
            percents.setdefault(row.enrollment_id, []).append(Decimal(row.mark) * 100 / row.task.max_marks)
        elif (
            missing_as_zero
            and row.is_missing(now)
            and (row.status == Submission.Status.NOT_DONE or row.task.hand_in == Task.HandIn.ONLINE)
        ):
            percents.setdefault(row.enrollment_id, []).append(Decimal(0))
    marks = {}
    for enrollment in students:
        found = percents.get(enrollment.pk)
        if found:
            marks[enrollment.pk] = (sum(found) / len(found) * Decimal(out_of) / 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            marks[enrollment.pk] = None
    return marks
