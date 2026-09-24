"""
Homework on the dashboards: what a teacher still has to check, and how much work the school's
students are handing in.
"""

from datetime import timedelta

from django.db.models import Count, F, Q
from django.utils import timezone

from .access import teaching_pairs
from .followup import UNKNOWN
from .models import Submission, Task, TaskSection

RECENT = timedelta(days=30)  # older unchecked work stops nagging


def to_check(user, school, academic_year, now=None, limit=8):
    """
    Each (task, section) of this teacher's that is waiting on them: work due and not yet
    checked in class, or handed in online and not yet returned. Newest due first.
    """
    now = now or timezone.now()
    pairs = teaching_pairs(user, school, academic_year)
    if not pairs:
        return [], 0
    mine = Q()
    for section_id, subject_id in pairs:
        mine |= Q(section_id=section_id, task__subject_id=subject_id)
    targets = (
        TaskSection.objects.filter(mine, school=school, task__status=Task.Status.PUBLISHED, due_at__lte=now)
        .filter(due_at__gte=now - RECENT)
        .annotate(
            unchecked=Count(
                "submissions",
                filter=Q(submissions__status=Submission.Status.PENDING, task__hand_in=Task.HandIn.IN_CLASS),
            ),
            to_return=Count(
                "submissions",
                filter=Q(submissions__source=Submission.Source.ONLINE)
                & (
                    Q(submissions__returned_at__isnull=True)
                    | Q(submissions__handed_in_at__gt=F("submissions__returned_at"))
                ),
            ),
        )
        .filter(Q(unchecked__gt=0) | Q(to_return__gt=0))
        .select_related("task__subject", "section__class_level")
        .order_by("-due_at")
    )
    rows = list(targets)
    return rows[:limit], len(rows)


def handed_in_rate(school, now=None, days=7):
    """
    Of the work due in the last `days` days whose outcome is known, the share handed in.
    Absent and excused students are left out.
    """
    from students.models import Enrollment, Student

    now = now or timezone.now()
    rows = Submission.objects.filter(
        school=school,
        task__status=Task.Status.PUBLISHED,
        target__due_at__gte=now - timedelta(days=days),
        target__due_at__lt=now,
        enrollment__status=Enrollment.Status.ENROLLED,
        enrollment__student__status=Student.Status.ACTIVE,
    ).exclude(status__in=Submission.NOT_EXPECTED)
    # Work checked in class that nobody has checked yet is unknown, not missing.
    rows = rows.exclude(UNKNOWN)
    totals = rows.aggregate(expected=Count("id"), handed=Count("id", filter=Q(status__in=Submission.HANDED_IN)))
    if not totals["expected"]:
        return None, 0
    return round(100 * totals["handed"] / totals["expected"]), totals["expected"]
