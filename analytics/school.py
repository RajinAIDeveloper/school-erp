"""
The whole school in one published exam, for the Principal, Vice Principal and Administrator:
each class and section against the others, every subject in every class, groups and boys and
girls, the top of each class, who has improved and who has slipped, the students at risk, and
the school's results across the year's exams.

Everything comes from the analytics tables, so it matches the published cards. Averages are of
students' percentages; a pass rate is only given where the rulebook has pass and fail.
"""

from collections import defaultdict
from decimal import Decimal

from django.db.models import Avg, Count, Max, Q

from examinations.models import Exam, ExamResultFact, SubjectResultFact
from examinations.rulebooks import rulebook

TOP = 10
TOP_PER_CLASS = 5


def _one(value):
    return None if value is None else Decimal(value).quantize(Decimal("0.1"))


def _rate(passed, judged):
    return _one(Decimal(100) * passed / judged) if judged else None


def _when(exam):
    return (exam.end_date or exam.start_date or exam.created_at.date(), exam.pk)


def school_overview(exam):
    facts = ExamResultFact.objects.filter(exam=exam)
    totals = facts.aggregate(
        students=Count("id"),
        average=Avg("percent"),
        judged=Count("id", filter=~Q(result="")),
        passed=Count("id", filter=Q(result="PASS")),
        failing=Count("id", filter=Q(subjects_failed__gt=0)),
        gpa=Avg("gpa"),
    )

    classes = [
        {
            "level": row["class_level__name"],
            "level_id": row["class_level_id"],
            "students": row["students"],
            "average": _one(row["average"]),
            "highest": _one(row["highest"]),
            "gpa": _one(row["gpa"]),
            "pass_rate": _rate(row["passed"], row["judged"]),
            "failing": row["failing"],
        }
        for row in facts.values("class_level_id", "class_level__name", "class_level__order")
        .annotate(
            students=Count("id"),
            average=Avg("percent"),
            highest=Max("percent"),
            gpa=Avg("gpa"),
            judged=Count("id", filter=~Q(result="")),
            passed=Count("id", filter=Q(result="PASS")),
            failing=Count("id", filter=Q(subjects_failed__gt=0)),
        )
        .order_by("class_level__order", "class_level__name")
    ]

    sections = defaultdict(list)
    for row in (
        facts.values("class_level_id", "section_id", "section__name")
        .annotate(
            students=Count("id"),
            average=Avg("percent"),
            judged=Count("id", filter=~Q(result="")),
            passed=Count("id", filter=Q(result="PASS")),
        )
        .order_by("section__name")
    ):
        sections[row["class_level_id"]].append(
            {
                "section_id": row["section_id"],
                "name": row["section__name"],
                "students": row["students"],
                "average": _one(row["average"]),
                "pass_rate": _rate(row["passed"], row["judged"]),
            }
        )
    for item in classes:
        item["sections"] = sections.get(item["level_id"], [])

    # Every subject in every class: the average percentage, and the pass rate where the class's
    # rulebook has pass marks. A Cambridge or IB subject is graded, not passed or failed.
    judged_levels = {
        row["class_level_id"]
        for row in facts.values("class_level_id", "rulebook").distinct()
        if rulebook(row["rulebook"]).pass_marks
    }
    heat, subject_names = defaultdict(dict), {}
    for row in (
        SubjectResultFact.objects.filter(exam=exam)
        .values("exam_fact__class_level_id", "subject_id", "subject_name")
        .annotate(
            average=Avg("percent"),
            students=Count("id"),
            judged=Count("id", filter=Q(passed__isnull=False)),
            passed=Count("id", filter=Q(passed=True)),
        )
    ):
        subject_names[row["subject_id"]] = row["subject_name"]
        heat[row["exam_fact__class_level_id"]][row["subject_id"]] = {
            "average": _one(row["average"]),
            "students": row["students"],
            "pass_rate": _rate(row["passed"], row["judged"])
            if row["exam_fact__class_level_id"] in judged_levels
            else None,
        }
    subjects = sorted(subject_names.items(), key=lambda item: item[1])
    grid = [
        {
            "level": item["level"],
            "level_id": item["level_id"],
            "cells": [(pk, heat[item["level_id"]].get(pk)) for pk, _n in subjects],
        }
        for item in classes
    ]

    groups = [
        {"name": row["group"], "students": row["students"], "average": _one(row["average"])}
        for row in facts.exclude(group="")
        .values("group")
        .annotate(students=Count("id"), average=Avg("percent"))
        .order_by("group")
    ]
    genders = [
        {
            "name": {"M": "Boys", "F": "Girls"}.get(row["student__gender"], "Other"),
            "students": row["students"],
            "average": _one(row["average"]),
        }
        for row in facts.values("student__gender")
        .annotate(students=Count("id"), average=Avg("percent"))
        .order_by("student__gender")
    ]

    # The top of each class: percentages from different classes, or different rulebooks, do not compare.
    for item in classes:
        item["top"] = list(
            facts.filter(class_level_id=item["level_id"], percent__isnull=False)
            .select_related("student", "section")
            .order_by("-percent", "student__first_name")[:TOP_PER_CLASS]
        )
        for fact in item["top"]:
            fact.shown = _one(fact.percent)
    improved, slipped = movers(exam)
    return {
        "exam": exam,
        "students": totals["students"],
        "average": _one(totals["average"]),
        "pass_rate": _rate(totals["passed"], totals["judged"]),
        "gpa": _one(totals["gpa"]),
        "failing": totals["failing"],
        "classes": classes,
        "subjects": subjects,
        "grid": grid,
        "groups": groups,
        "genders": genders,
        "improved": improved,
        "slipped": slipped,
        "trend": year_trend(exam),
    }


def movers(exam):
    """The students whose percentage rose or fell most since their previous published exam this year."""
    now = {
        f.student_id: f
        for f in ExamResultFact.objects.filter(exam=exam, percent__isnull=False).select_related(
            "student", "section", "class_level"
        )
    }
    earlier = (
        ExamResultFact.objects.filter(student_id__in=now, academic_year=exam.academic_year, percent__isnull=False)
        .exclude(exam=exam)
        .select_related("exam")
    )
    before = {}
    for fact in earlier:
        if _when(fact.exam) >= _when(exam):
            continue
        if fact.student_id not in before or _when(fact.exam) > _when(before[fact.student_id].exam):
            before[fact.student_id] = fact
    changes = [
        {"fact": now[pk], "before": before[pk], "change": _one(now[pk].percent - before[pk].percent)} for pk in before
    ]
    improved = sorted((c for c in changes if c["change"] > 0), key=lambda c: -c["change"])[:TOP]
    slipped = sorted((c for c in changes if c["change"] < 0), key=lambda c: c["change"])[:TOP]
    return improved, slipped


def year_trend(exam):
    """Each published exam of the year in order, with the school average and each class's average."""
    exams = sorted(
        Exam.objects.filter(school=exam.school, academic_year=exam.academic_year, status="published"), key=_when
    )
    rows = (
        ExamResultFact.objects.filter(exam__in=exams)
        .values("exam_id", "class_level__name")
        .annotate(average=Avg("percent"))
    )
    overall = {
        row["exam_id"]: _one(row["average"])
        for row in ExamResultFact.objects.filter(exam__in=exams).values("exam_id").annotate(average=Avg("percent"))
    }
    by_class = defaultdict(dict)
    for row in rows:
        by_class[row["class_level__name"]][row["exam_id"]] = _one(row["average"])
    # Only the exams a class actually sat carry a point. A school line is drawn only when every
    # exam was sat by the same classes: averaging a Class 9 exam against an IGCSE mock would
    # compare different children under different rules.
    same_classes = all(len(values) == len(exams) for values in by_class.values())
    school_line = [("School", [overall.get(e.pk) for e in exams])] if same_classes and len(by_class) > 1 else []
    return {
        "labels": [e.name for e in exams],
        "series": school_line + sorted((name, [values.get(e.pk) for e in exams]) for name, values in by_class.items()),
    }
