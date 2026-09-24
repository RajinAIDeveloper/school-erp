"""
Exam timetable clashes: two papers of one class at overlapping times that a student sits both.

Papers that overlap but that no student sits together are fine, and deliberate: an IGCSE option
block runs its subjects at the same time because each student takes only one of them.
"""


def overlap(a, b):
    """Whether two papers' dates and times overlap. Papers without both times are not judged."""
    if not (a.date and b.date and a.date == b.date):
        return False
    if not (a.start_time and a.end_time and b.start_time and b.end_time):
        return False
    return a.start_time < b.end_time and b.start_time < a.end_time


def sitting_both(first, second):
    """The students of the class who sit both papers, by the class's subject plan."""
    from students.models import Enrollment

    from .subjects import paper_role, subject_plan

    exam = first.exam
    plan = subject_plan(exam.academic_year, first.class_level)
    enrollments = (
        Enrollment.objects.filter(academic_year=exam.academic_year, class_level=first.class_level)
        .exclude(status=Enrollment.Status.LEFT)
        .select_related("student")
        .prefetch_related("chosen_subjects")
    )
    return [e for e in enrollments if paper_role(e, first.subject, plan) and paper_role(e, second.subject, plan)]


def clash_message(first, second, students):
    names = ", ".join(e.student.full_name for e in students[:3])
    more = f" and {len(students) - 3} more" if len(students) > 3 else ""
    return (
        f"{first.class_level}: {first.subject} and {second.subject} overlap on {first.date:%d %b %Y}, "
        f"and {len(students)} student(s) sit both ({names}{more})."
    )


def clashes_for(schedule, others):
    """Messages for each paper in `others` that clashes with `schedule`."""
    found = []
    for other in others:
        if other.pk == schedule.pk or other.class_level_id != schedule.class_level_id or not overlap(schedule, other):
            continue
        students = sitting_both(schedule, other)
        if students:
            found.append(clash_message(schedule, other, students))
    return found


def timetable_clashes(exam):
    schedules = list(exam.schedules.select_related("subject", "class_level", "exam__academic_year"))
    found = []
    for index, schedule in enumerate(schedules):
        found.extend(clashes_for(schedule, schedules[index + 1 :]))
    return found
