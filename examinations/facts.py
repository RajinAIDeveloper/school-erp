"""
Published results laid out for analysis.

Analytics read these tables, never the live marks and never the snapshots' JSON: the rows are
written from exactly what was published, whenever an exam is published or republished, so a
chart can never disagree with a report card. Each publication replaces the exam's rows as a
whole, inside the same transaction as the snapshot.
"""

from decimal import Decimal, InvalidOperation

from django.db import transaction

from .grading import headline
from .models import ExamResultFact, ResultSnapshot, SubjectResultFact


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _units(row):
    """The subjects a card grades. Results published before combined subjects existed grade each paper."""
    if row.get("subjects"):
        return row["subjects"]
    return [
        {
            "name": cell["subject"],
            "papers": [cell["schedule_id"]],
            "full_marks": cell["full_marks"],
            "score": cell["score"],
            "percent": cell.get("percent"),
            "absent": cell["absent"],
            "letter": cell["letter"],
            "grade_point": cell["grade_point"],
            "passed": cell.get("passed"),
            "is_fourth": cell.get("is_fourth", False),
        }
        for cell in row["cells"]
        if not cell.get("exempt")
    ]


@transaction.atomic
def record_exam(exam, rows, version):
    """Replace the exam's analysis rows with those of one published version."""
    ExamResultFact.objects.filter(exam=exam).delete()
    facts, subject_rows = [], []
    for row in rows:
        units = _units(row)
        cells = {cell["schedule_id"]: cell for cell in row["cells"]}
        failed = sum(
            1 for unit in units if unit.get("passed") is False and not unit.get("is_fourth") and not unit.get("missing")
        )
        fact = ExamResultFact(
            school=exam.school,
            exam=exam,
            enrollment_id=row["enrollment_id"],
            student_id=row["student_id"],
            academic_year_id=exam.academic_year_id,
            class_level_id=row["class_level_id"],
            section_id=row.get("section_id"),
            version=version,
            rulebook=row.get("system") or "own",
            group=row.get("group") or "",
            total=_decimal(row.get("total")),
            full_total=_decimal(row.get("full_total")),
            percent=_decimal(row.get("percent")),
            gpa=_decimal(row.get("gpa")) if row.get("has_gpa", True) else None,
            points=_decimal(row.get("points")),
            result=row.get("result") or "",
            headline=(headline(row) or "")[:200],
            section_rank=row.get("rank"),
            class_rank=row.get("grade_rank"),
            show_rank=row.get("show_rank", True),
            subjects_failed=failed,
        )
        facts.append(fact)
        for unit in units:
            papers = [pk for pk in unit.get("papers") or [] if pk in cells]
            if not papers:
                continue
            subject_rows.append(
                (
                    fact,
                    SubjectResultFact(
                        school=exam.school,
                        exam=exam,
                        enrollment_id=row["enrollment_id"],
                        student_id=row["student_id"],
                        section_id=row.get("section_id"),
                        subject_id=cells[papers[0]].get("unit_id") or cells[papers[0]]["subject_id"],
                        subject_name=unit["name"],
                        schedule_id=papers[0] if len(papers) == 1 else None,
                        papers=len(papers),
                        score=_decimal(unit.get("score")),
                        full_marks=_decimal(unit.get("full_marks")),
                        percent=_decimal(unit.get("percent")),
                        letter=unit.get("letter") or "",
                        grade_point=_decimal(unit.get("grade_point")),
                        passed=unit.get("passed"),
                        absent=bool(unit.get("absent")),
                        is_fourth=bool(unit.get("is_fourth")),
                    ),
                )
            )
    ExamResultFact.objects.bulk_create(facts)
    for fact, subject in subject_rows:
        subject.exam_fact = fact
    SubjectResultFact.objects.bulk_create([subject for _fact, subject in subject_rows])
    return len(facts)


def rebuild(exam):
    """Write the exam's analysis rows again from its current published snapshots."""
    if exam.status != "published" or not exam.publication_version:
        ExamResultFact.objects.filter(exam=exam).delete()
        return 0
    snapshots = ResultSnapshot.objects.filter(exam=exam, version=exam.publication_version)
    return record_exam(exam, [snapshot.payload for snapshot in snapshots], exam.publication_version)
