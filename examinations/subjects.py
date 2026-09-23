"""
Which papers each student sits.

Up to Class 8 everyone in a class usually sits the same papers, and a class with no subject
plan keeps working exactly that way. From Class 9 the national curriculum splits students into
Science, Business Studies and Humanities, lets each choose a 4th subject, and gives each
religion its own paper. A Humanities student never sits Physics, so a result that expects a
Physics mark from every student could never be published. This module answers, for one
student, which of an exam's papers are theirs.
"""

from academics.models import ClassSubject


class SubjectPlan:
    """The subject rows for one class in one year, read once and asked many times."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.planned = {row.subject_id for row in self.rows}

    def _ids(self, kind, group):
        return {row.subject_id for row in self.rows if row.kind == kind and row.group in ("", group or "")}

    def compulsory_for(self, group):
        return self._ids(ClassSubject.Kind.COMPULSORY, group)

    def choices_for(self, group):
        return self._ids(ClassSubject.Kind.CHOICE, group)

    def groups(self):
        return sorted({row.group for row in self.rows if row.group})


def subject_plan(academic_year, class_level):
    """The class's plan, or None when it has none and everyone sits every paper."""
    if academic_year is None or class_level is None:
        return None
    rows = list(
        ClassSubject.objects.filter(academic_year=academic_year, class_level=class_level).select_related("subject")
    )
    return SubjectPlan(rows) if rows else None


def _chosen_ids(enrollment):
    prefetched = getattr(enrollment, "_prefetched_objects_cache", {}).get("chosen_subjects")
    if prefetched is not None:
        return {subject.pk for subject in prefetched}
    return set(enrollment.chosen_subjects.values_list("pk", flat=True))


def paper_role(enrollment, subject, plan):
    """
    How this student takes one subject: "main", "fourth", or None when it is not theirs.

    A subject scheduled but missing from the plan is treated as compulsory, so a school that
    adds an extra paper and forgets the plan does not silently drop it from every result.
    """
    if subject.religion and subject.religion != (enrollment.student.religion or ""):
        return None
    if subject.pk == enrollment.fourth_subject_id:
        return "fourth"
    if plan is None or subject.pk not in plan.planned:
        return "main"
    if subject.pk in plan.compulsory_for(enrollment.group):
        return "main"
    if subject.pk in plan.choices_for(enrollment.group) and subject.pk in _chosen_ids(enrollment):
        return "main"
    return None


def papers_for(enrollment, schedules, plan):
    """[(schedule, role)] for the papers this student sits, in the order given."""
    taken = []
    for schedule in schedules:
        role = paper_role(enrollment, schedule.subject, plan)
        if role:
            taken.append((schedule, role))
    return taken


def takes_paper(enrollment, schedule, plan=None):
    if plan is None:
        plan = subject_plan(schedule.exam.academic_year, schedule.class_level)
    return paper_role(enrollment, schedule.subject, plan) is not None


def enrollments_taking(schedule, enrollments, plan=None):
    """Only the students who sit this paper, from a list the caller already loaded."""
    if plan is None:
        plan = subject_plan(schedule.exam.academic_year, schedule.class_level)
    return [enrollment for enrollment in enrollments if paper_role(enrollment, schedule.subject, plan)]


def check_choices(enrollment, plan):
    """
    Problems with one student's group and subject choices, as sentences.

    Used by the groups screen to show what still needs fixing, and by the save to refuse a
    combination the plan does not allow.
    """
    problems = []
    if plan is None:
        return problems
    groups = plan.groups()
    if groups and not enrollment.group:
        problems.append("No group chosen.")
        return problems
    if groups and enrollment.group not in groups:
        problems.append("That group is not offered in this class.")
        return problems
    allowed = plan.choices_for(enrollment.group)
    chosen = _chosen_ids(enrollment)
    if chosen - allowed:
        problems.append("A chosen subject is not offered to this group.")
    fourth = enrollment.fourth_subject_id
    if fourth and fourth not in allowed:
        problems.append("The 4th subject is not one this group may take.")
    if fourth and fourth in chosen:
        problems.append("The 4th subject cannot also be a main subject.")
    religion_papers = {row.subject_id for row in plan.rows if row.subject.religion}
    if religion_papers and not any(
        row.subject.religion == (enrollment.student.religion or "") for row in plan.rows if row.subject.religion
    ):
        problems.append("The student's religion is not recorded, so no religion paper applies.")
    return problems
