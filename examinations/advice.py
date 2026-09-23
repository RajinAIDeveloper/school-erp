"""
What a published result suggests for promotion: move up, or hold back for the school to decide.

Only a published exam or combined result is used, so advice never rests on marks that were
never released. Advice is advice: the school decides, and any decision against it needs a
reason, which is recorded.
"""

from django.db.models import F

from .grading import headline
from .models import CombinedResult, CombinedSnapshot, Exam, ResultSnapshot


def basis_choices(school, academic_year):
    """Published exams and combined results of a year, as (value, label) for a select."""
    choices = [("", "No result: decide for each student")]
    for exam in Exam.objects.filter(school=school, academic_year=academic_year, status="published").order_by("name"):
        choices.append((f"exam-{exam.pk}", f"Exam: {exam.name}"))
    for combined in CombinedResult.objects.filter(school=school, academic_year=academic_year, status="published"):
        choices.append((f"combined-{combined.pk}", f"Combined: {combined.name}"))
    return choices


def published_rows(school, basis):
    """{enrollment id: published payload} for a basis value such as "exam-4", or None if it is not one."""
    kind, _, raw = (basis or "").partition("-")
    if not raw.isdigit():
        return None, ""
    if kind == "exam":
        exam = Exam.objects.filter(school=school, pk=int(raw), status="published").first()
        if exam is None:
            return None, ""
        snapshots = ResultSnapshot.objects.filter(exam=exam, version=exam.publication_version)
        return {s.enrollment_id: s.payload for s in snapshots}, exam.name
    if kind == "combined":
        combined = CombinedResult.objects.filter(school=school, pk=int(raw), status="published").first()
        if combined is None:
            return None, ""
        snapshots = CombinedSnapshot.objects.filter(combined=combined, version=F("combined__publication_version"))
        return {s.enrollment_id: s.payload for s in snapshots}, combined.name
    return None, ""


def advise(row):
    """(promote?, why) for one student's published result, or for none."""
    if row is None:
        return False, "No published result"
    result = row.get("result")
    if result == "PASS":
        return True, f"Passed ({headline(row)})"
    if result == "FAIL":
        return False, f"Failed ({headline(row)})"
    if not row.get("complete", True):
        return False, "Result incomplete"
    # Grade-only and IB rulebooks have no pass or fail: the school's own policy decides.
    return True, f"{headline(row)}: no pass or fail under {row.get('rulebook') or 'this rulebook'}, check school policy"
