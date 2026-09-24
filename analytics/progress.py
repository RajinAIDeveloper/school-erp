"""
One student's progress: every published exam, each subject against the class, the trend over
time, what is going well, what needs attention, and attendance month by month.

Only published results are used, from the analytics tables, so every figure matches a card.
Class averages and highest marks are for the student's whole class (every section of the class
level) in that exam, as the report card's "Highest" column is; no other student is ever named.
What is shown is cut to the viewer's scope: a subject teacher sees only their subjects.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg, Count, Max, Q

from examinations.models import ExamResultFact, SubjectResultFact

# How far from the class average (in percentage points) counts as a strength or a concern.
MARGIN = Decimal(10)


def _round(value):
    return None if value is None else Decimal(value).quantize(Decimal("0.1"))


def _exam_order(fact):
    exam = fact.exam
    return (exam.end_date or exam.start_date or exam.created_at.date(), exam.published_at or exam.created_at, exam.pk)


def student_progress(student, scope):
    """Everything the progress page and report show for one student, within `scope`."""
    overall = list(
        scope.results(ExamResultFact.objects.filter(student=student)).select_related("exam", "class_level", "section")
    )
    subjects = list(
        scope.subject_results(SubjectResultFact.objects.filter(student=student)).select_related(
            "exam", "exam_fact__class_level", "exam_fact__exam"
        )
    )
    facts_by_exam = {fact.exam_id: fact for fact in overall}
    for subject in subjects:
        facts_by_exam.setdefault(subject.exam_id, subject.exam_fact)
    exams = sorted(facts_by_exam.values(), key=_exam_order)
    if not exams:
        return None
    pairs = Q(pk__in=[])
    for fact in exams:
        pairs |= Q(exam_id=fact.exam_id, class_level_id=fact.class_level_id)

    class_overall = {
        row["exam_id"]: row
        for row in ExamResultFact.objects.filter(pairs)
        .values("exam_id")
        .annotate(average=Avg("percent"), highest=Max("percent"), size=Count("id"))
    }
    subject_pairs = Q(pk__in=[])
    for fact in exams:
        subject_pairs |= Q(exam_id=fact.exam_id, exam_fact__class_level_id=fact.class_level_id)
    class_subjects = {
        (row["exam_id"], row["subject_id"]): row
        for row in SubjectResultFact.objects.filter(subject_pairs)
        .values("exam_id", "subject_id")
        .annotate(average=Avg("percent"), highest=Max("percent"))
    }

    shown_overall = {fact.exam_id for fact in overall}
    exam_rows = []
    for fact in exams:
        stats = class_overall.get(fact.exam_id, {})
        full = fact.exam_id in shown_overall
        exam_rows.append(
            {
                "exam": fact.exam,
                "class": fact.class_level,
                "percent": fact.percent if full else None,
                "gpa": fact.gpa if full else None,
                "points": fact.points if full else None,
                "result": fact.result if full else "",
                "headline": fact.headline if full else "",
                "rank": fact.section_rank if full and fact.show_rank else None,
                "class_rank": fact.class_rank if full and fact.show_rank else None,
                "failed": fact.subjects_failed if full else None,
                "average": _round(stats.get("average")),
                "highest": _round(stats.get("highest")),
                "size": stats.get("size"),
            }
        )

    by_exam = defaultdict(list)
    for subject in subjects:
        by_exam[subject.exam_id].append(subject)
    latest = exams[-1]
    latest_rows = []
    for subject in sorted(by_exam[latest.exam_id], key=lambda s: s.subject_name):
        stats = class_subjects.get((subject.exam_id, subject.subject_id), {})
        latest_rows.append(
            {
                "subject": subject.subject_name,
                "subject_id": subject.subject_id,
                "score": subject.score,
                "full": subject.full_marks,
                "percent": _round(subject.percent),
                "letter": subject.letter,
                "passed": subject.passed,
                "absent": subject.absent,
                "average": _round(stats.get("average")),
                "highest": _round(stats.get("highest")),
            }
        )

    # Each subject's percentage in each exam, in exam order.
    names = {}
    series = defaultdict(dict)
    for subject in subjects:
        names[subject.subject_id] = subject.subject_name
        series[subject.subject_id][subject.exam_id] = _round(subject.percent)
    order = [fact.exam_id for fact in exams]
    trends = {
        "labels": [fact.exam.name for fact in exams],
        "overall": [row["percent"] for row in exam_rows],
        "average": [row["average"] for row in exam_rows],
        "subjects": sorted(
            ((names[pk], [series[pk].get(exam_id) for exam_id in order]) for pk in series), key=lambda s: s[0]
        ),
    }

    strengths, attention = [], []
    previous = {}
    if len(exams) > 1:
        before = exams[-2].exam_id
        previous = {s.subject_id: _round(s.percent) for s in by_exam[before]}
    for row in latest_rows:
        if row["absent"]:
            attention.append((row["subject"], "absent"))
            continue
        if row["percent"] is None:
            continue
        if row["passed"] is False:
            attention.append((row["subject"], "failed"))
        elif row["average"] is not None and row["percent"] <= row["average"] - MARGIN:
            attention.append((row["subject"], "below the class average"))
        elif row["subject_id"] in previous and previous[row["subject_id"]] is not None:
            if row["percent"] <= previous[row["subject_id"]] - MARGIN:
                attention.append((row["subject"], "down since the last exam"))
        if row["average"] is not None and row["percent"] >= row["average"] + MARGIN:
            strengths.append((row["subject"], row["percent"] - row["average"]))
    strengths = [name for name, _gap in sorted(strengths, key=lambda item: -item[1])[:3]]

    return {
        "student": student,
        "exams": exam_rows,
        "latest": exam_rows[-1],
        "latest_subjects": latest_rows,
        "trends": trends,
        "strengths": strengths,
        "attention": attention,
        "attendance": monthly_attendance(latest.enrollment),
        "overall_shown": bool(shown_overall),
    }


def monthly_attendance(enrollment):
    """[(first day of month, present, total, percent)] for the enrollment's year, oldest first."""
    from django.db.models.functions import TruncMonth

    from attendance.models import StudentAttendance

    rows = (
        StudentAttendance.objects.filter(enrollment=enrollment)
        .annotate(month=TruncMonth("date"))
        .values("month")
        .annotate(total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"])))
        .order_by("month")
    )
    return [
        (
            row["month"],
            row["present"],
            row["total"],
            round(row["present"] * 100 / row["total"]) if row["total"] else None,
        )
        for row in rows
    ]
