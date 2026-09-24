"""
One subject in one exam, for the teachers who teach it: how the marks spread, how each section
did, how the parts of the paper went, and every student's mark with the change since the last
exam and anything that needs following up.

A published exam is read from the analytics tables, exactly as published. An exam still being
marked is worked out live from the marks entered so far, and says it is a draft.
"""

import statistics
from collections import Counter, defaultdict
from decimal import Decimal

from examinations.grading import scale_rules
from examinations.models import ExamResultFact, ExamSchedule, Mark, SubjectResultFact

NEAR = Decimal(5)  # within this many marks of the pass mark counts as near
DROP = Decimal(10)  # a fall of this many percentage points since the last exam is flagged


def _one(value):
    return None if value is None else Decimal(value).quantize(Decimal("0.1"))


def allowed_sections(exam, subject, scope):
    """The sections of this exam that sit the subject and that the viewer may analyse it for."""
    from academics.models import Section

    levels = (
        ExamSchedule.objects.filter(exam=exam)
        .filter(subject__in=[subject.pk, *subject.papers.values_list("pk", flat=True)])
        .values_list("class_level_id", flat=True)
    )
    sections = Section.objects.filter(school=exam.school, class_level_id__in=levels).select_related("class_level")
    return [
        section
        for section in sections.order_by("class_level__order", "name")
        if scope.sees_subject(section.pk, subject.pk)
    ]


def papers_of(exam, subject):
    """The exam's papers that make up this subject: one, or two graded together (Bangla 1st and 2nd)."""
    return list(
        ExamSchedule.objects.filter(exam=exam)
        .filter(subject__in=[subject.pk, *subject.papers.values_list("pk", flat=True)])
        .select_related("subject")
        .prefetch_related("components")
        .order_by("subject__code", "subject__name")
    )


def _published_rows(exam, subject, sections):
    facts = (
        SubjectResultFact.objects.filter(exam=exam, subject=subject, section__in=sections)
        .select_related("enrollment__student", "section__class_level")
        .order_by("section__name", "enrollment__roll_number")
    )
    return [
        {
            "enrollment": fact.enrollment,
            "section": fact.section,
            "score": fact.score,
            "full": fact.full_marks,
            "percent": _one(fact.percent),
            "letter": fact.letter,
            "passed": fact.passed,
            "absent": fact.absent,
            "missing": False,
        }
        for fact in facts
    ]


def _draft_rows(exam, subject, sections, papers):
    from examinations.services import live_class_sheet
    from students.models import Enrollment

    paper_ids = {paper.pk for paper in papers}
    by_pk = {
        e.pk: e
        for e in Enrollment.objects.filter(section__in=sections, academic_year=exam.academic_year).select_related(
            "student", "section__class_level"
        )
    }
    rows = []
    for level in {section.class_level for section in sections}:
        for row in live_class_sheet(exam, level):
            enrollment = by_pk.get(row["enrollment_id"])
            if enrollment is None:
                continue
            for unit in row.get("subjects") or []:
                if not paper_ids & set(unit.get("papers") or []):
                    continue
                rows.append(
                    {
                        "enrollment": enrollment,
                        "section": enrollment.section,
                        "score": Decimal(unit["score"]) if unit.get("score") not in (None, "") else None,
                        "full": Decimal(str(unit["full_marks"])),
                        "percent": _one(unit.get("percent")) if not unit.get("missing") else None,
                        "letter": "" if unit.get("missing") else unit.get("letter", ""),
                        "passed": None if unit.get("missing") else unit.get("passed"),
                        "absent": bool(unit.get("absent")),
                        "missing": bool(unit.get("missing")),
                    }
                )
    return sorted(rows, key=lambda r: (r["section"].name, r["enrollment"].roll_number))


def _previous(exam, subject, enrollments):
    """Each student's percentage in the subject in their latest published exam before this one."""
    earlier = (
        SubjectResultFact.objects.filter(
            subject=subject, enrollment__in=enrollments, exam__academic_year=exam.academic_year
        )
        .exclude(exam=exam)
        .select_related("exam")
    )
    when = exam.end_date or exam.start_date
    best = {}
    for fact in earlier:
        other = fact.exam.end_date or fact.exam.start_date
        if when and other and other >= when:
            continue
        key = (other or fact.exam.created_at.date(), fact.exam_id)
        if fact.enrollment_id not in best or key > best[fact.enrollment_id][0]:
            best[fact.enrollment_id] = (key, _one(fact.percent), fact.exam.name)
    return {pk: (percent, name) for pk, (_key, percent, name) in best.items()}


def _letters(exam):
    rules = exam.grading_snapshot or (scale_rules(exam.grade_scale) if exam.grade_scale_id else [])
    return [r["letter"] for r in sorted(rules, key=lambda r: Decimal(str(r["min_percent"])), reverse=True)]


def paper_analysis(exam, subject, sections):
    """Everything the paper page shows. `sections` must already be cut to what the viewer may see."""
    from examinations.rulebooks import rulebook

    papers = papers_of(exam, subject)
    draft = exam.status != "published"
    rows = _draft_rows(exam, subject, sections, papers) if draft else _published_rows(exam, subject, sections)
    # Pass marks, pass rates and "only just passed" mean something only under a rulebook that has
    # pass marks; a Cambridge or IB paper is graded, not passed or failed.
    has_pass = rulebook(exam.rules_for(sections[0].class_level)).pass_marks
    if not has_pass:
        for row in rows:
            row["passed"] = None
    pass_mark = sum((Decimal(str(p.pass_marks)) for p in papers), Decimal(0))
    sat = [r for r in rows if not r["absent"] and not r["missing"] and r["percent"] is not None]
    percents = [float(r["percent"]) for r in sat]
    judged = [r for r in sat if r["passed"] is not None]

    previous = _previous(exam, subject, [r["enrollment"] for r in rows])
    for row in rows:
        before = previous.get(row["enrollment"].pk)
        row["before"] = before[0] if before else None
        row["change"] = (
            row["percent"] - before[0] if before and before[0] is not None and row["percent"] is not None else None
        )
        flags = []
        if row["absent"]:
            flags.append("absent")
        elif row["missing"]:
            flags.append("no mark yet")
        else:
            if row["passed"] is False:
                flags.append("failed")
            elif row["passed"] and row["score"] is not None and row["score"] - pass_mark <= NEAR:
                flags.append("only just passed")
            if row["change"] is not None and row["change"] <= -DROP:
                flags.append("down since the last exam")
        row["flags"] = flags

    bins = [0] * 10
    for value in percents:
        bins[min(int(value // 10), 9)] += 1

    by_section = defaultdict(list)
    for row in sat:
        by_section[row["section"]].append(row)
    sections_summary = [
        {
            "section": section,
            "sat": len(group),
            "average": _one(statistics.mean(float(r["percent"]) for r in group)),
            "pass_rate": _one(100 * sum(1 for r in group if r["passed"]) / len(group))
            if any(r["passed"] is not None for r in group)
            else None,
        }
        for section, group in sorted(by_section.items(), key=lambda item: (item[0].class_level.order, item[0].name))
    ]

    letters = _letters(exam)
    counts = Counter(r["letter"] for r in sat if r["letter"])
    grades = [(letter, counts.get(letter, 0)) for letter in letters] + [
        (letter, n) for letter, n in counts.items() if letter not in letters
    ]

    return {
        "exam": exam,
        "subject": subject,
        "draft": draft,
        "papers": papers,
        "pass_mark": pass_mark if judged else None,
        "rows": rows,
        "entered": len(rows) - sum(1 for r in rows if r["missing"]),
        "students": len(rows),
        "sat": len(sat),
        "absent": sum(1 for r in rows if r["absent"]),
        "missing": sum(1 for r in rows if r["missing"]),
        "average": _one(statistics.mean(percents)) if percents else None,
        "median": _one(statistics.median(percents)) if percents else None,
        "spread": _one(statistics.pstdev(percents)) if len(percents) > 1 else None,
        "highest": _one(max(percents)) if percents else None,
        "lowest": _one(min(percents)) if percents else None,
        "pass_rate": _one(100 * sum(1 for r in judged if r["passed"]) / len(judged)) if judged else None,
        "bins": [(f"{10 * i}–{10 * i + 9 if i < 9 else 100}", count) for i, count in enumerate(bins)],
        "sections": sections_summary,
        "grades": grades,
        "parts": part_averages(papers, [r["enrollment"] for r in rows]),
        "flagged": sum(1 for r in rows if r["flags"]),
    }


def part_averages(papers, enrollments):
    """
    The class's average in each part of the paper, as a percentage of that part's full marks:
    creative against multiple choice, theory against practical. For a subject graded on two
    papers, each paper is a part.
    """
    marks = Mark.objects.filter(schedule__in=papers, enrollment__in=enrollments, is_absent=False, is_exempt=False)
    by_paper = defaultdict(list)
    for mark in marks:
        by_paper[mark.schedule_id].append(mark)
    parts = []
    for paper in papers:
        components = list(paper.components.all())
        entered = by_paper.get(paper.pk, [])
        if components:
            for component in components:
                scores = [
                    Decimal(str(m.component_marks[component.code]))
                    for m in entered
                    if (m.component_marks or {}).get(component.code) not in (None, "")
                ]
                if scores and component.full_marks:
                    average = sum(scores) / len(scores)
                    # With two papers, say which paper a part belongs to.
                    name = f"{paper.subject.name}: {component.name}" if len(papers) > 1 else component.name
                    parts.append((name, _one(average * 100 / Decimal(str(component.full_marks))), len(scores)))
        elif len(papers) > 1 and paper.full_marks:
            scores = [m.marks_obtained for m in entered if m.marks_obtained is not None]
            if scores:
                average = sum(scores) / len(scores)
                parts.append((paper.subject.name, _one(average * 100 / Decimal(str(paper.full_marks))), len(scores)))
    return parts


def section_grid(exam, section):
    """
    A class teacher's view of one section in one published exam: every student against every
    subject, with the overall result, and the section's average in each subject.
    """
    from examinations.rulebooks import rulebook

    facts = list(
        ExamResultFact.objects.filter(exam=exam, section=section)
        .select_related("enrollment__student")
        .order_by("enrollment__roll_number")
    )
    has_pass = bool(facts) and rulebook(facts[0].rulebook).pass_marks
    subject_facts = SubjectResultFact.objects.filter(exam_fact__in=facts)
    names, cells = {}, defaultdict(dict)
    for fact in subject_facts:
        names[fact.subject_id] = fact.subject_name
        cells[fact.enrollment_id][fact.subject_id] = fact
    columns = sorted(names.items(), key=lambda item: item[1])
    rows = [
        {
            "fact": fact,
            "cells": [cells[fact.enrollment_id].get(pk) for pk, _name in columns],
        }
        for fact in facts
    ]
    averages = []
    for pk, _name in columns:
        values = [
            cells[f.enrollment_id][pk].percent
            for f in facts
            if pk in cells[f.enrollment_id] and cells[f.enrollment_id][pk].percent is not None
        ]
        passed = [cells[f.enrollment_id][pk].passed for f in facts if pk in cells[f.enrollment_id]]
        judged = [p for p in passed if p is not None] if has_pass else []
        averages.append(
            {
                "average": _one(sum(values) / len(values)) if values else None,
                "pass_rate": _one(100 * sum(1 for p in judged if p) / len(judged)) if judged else None,
            }
        )
    overall = [f.percent for f in facts if f.percent is not None]
    return {
        "exam": exam,
        "section": section,
        "columns": columns,
        "rows": rows,
        "averages": averages,
        "average": _one(sum(overall) / len(overall)) if overall else None,
        "show_rank": any(f.show_rank for f in facts),
        "has_gpa": any(f.gpa is not None for f in facts),
    }
