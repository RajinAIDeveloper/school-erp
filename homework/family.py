"""
Homework as a student and their family see it: a to-do list by when work is due, and the
counts on each child's card.
"""

from datetime import timedelta

from django.db.models import Q
from django.http import Http404
from django.utils import timezone

from .models import Submission, Task
from .services import school_week, sync_student

RECENT_DAYS = 30  # how far back finished work stays on the list


def family_students(user, school):
    """The student this account is, or the children it is guardian of. Nobody else's."""
    from students.models import Student

    return (
        Student.objects.filter(school=school).filter(Q(user=user) | Q(guardian_links__guardian__user=user)).distinct()
    )


def live_rows(enrollment, now):
    return (
        Submission.objects.filter(enrollment=enrollment, task__status=Task.Status.PUBLISHED, task__publish_at__lte=now)
        .select_related("task__subject", "task__teacher", "target")
        .order_by("target__due_at", "task__title")
    )


def family_submission(user, school, pk, now=None):
    """One record the family may open: their own child's, of work that is out. Else "not found"."""
    now = now or timezone.now()
    row = (
        Submission.objects.filter(
            pk=pk,
            school=school,
            enrollment__student__in=family_students(user, school),
            task__status=Task.Status.PUBLISHED,
            task__publish_at__lte=now,
        )
        .select_related("task__subject", "task__teacher", "target__section", "enrollment__student")
        .first()
    )
    if row is None:
        raise Http404
    return row


def week_ends(school, today):
    """The last day of this school week and of the next."""
    week = school_week(school, today)
    end = max(week) if week else today
    return end, end + timedelta(days=7)


def todo(enrollment, now=None):
    """The child's homework, grouped by when it is due. Finished work stays for a month."""
    now = now or timezone.now()
    sync_student(enrollment, now)
    today = timezone.localdate(now)
    this_week, next_week = week_ends(enrollment.school, today)
    groups = {name: [] for name in ("overdue", "soon", "week", "next", "later", "done")}
    for row in live_rows(enrollment, now):
        day = timezone.localdate(row.due_at)
        if row.status == Submission.Status.NOT_DONE or (row.status == Submission.Status.PENDING and row.due_at <= now):
            groups["overdue"].append(row)
        elif row.status == Submission.Status.PENDING:
            if day <= today + timedelta(days=1):
                groups["soon"].append(row)
            elif day <= this_week:
                groups["week"].append(row)
            elif day <= next_week:
                groups["next"].append(row)
            else:
                groups["later"].append(row)
        elif day >= today - timedelta(days=RECENT_DAYS) or day > today:
            groups["done"].append(row)
    groups["done"].sort(key=lambda row: row.due_at, reverse=True)
    return groups


def due_counts(enrollment, now=None):
    """For a child's card: work still to do this school week, and work overdue."""
    now = now or timezone.now()
    today = timezone.localdate(now)
    this_week, _next = week_ends(enrollment.school, today)
    # Work due tomorrow counts even when tomorrow is past the end of the school week.
    horizon = max(this_week, today + timedelta(days=1))
    due, overdue = 0, 0
    for row in live_rows(enrollment, now).filter(status__in=[Submission.Status.PENDING, Submission.Status.NOT_DONE]):
        if row.status == Submission.Status.NOT_DONE or row.due_at <= now:
            overdue += 1
        elif timezone.localdate(row.due_at) <= horizon:
            due += 1
    return {"due": due, "overdue": overdue}
