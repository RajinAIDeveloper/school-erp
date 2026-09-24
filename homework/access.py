"""
Who may reach homework, and who may set, check and see each task.

Every homework screen sits behind the module: a school the platform administrator has not
given homework gets "not found" on all of them, whoever asks. Inside, a teacher works on the
sections and subjects they teach (co-teachers alike), a class teacher may look at their
section's work but not mark it, and the school's managers may do everything.

A teacher of one paper (Bangla 1st paper) may set work for that paper or for the subject as a
whole; a teacher of the whole subject may set work for any of its papers.
"""

from django.contrib.auth.decorators import login_required
from django.http import Http404

from core.access import is_manager, require_permission
from core.modules import require_module


def homework_view(permission, *, also=""):
    """
    Guard a homework view. Signed out: the sign-in page. Signed in to a school without the
    module: "not found", before any permission is looked at, for every role.
    """

    def decorate(view):
        narrowing = f"{also}; homework module only" if also else "homework module only"
        guarded = require_permission(permission, also=narrowing)(view)
        return login_required(require_module("homework")(guarded))

    return decorate


def employee_of(user):
    return getattr(user, "employee_profile", None)


def teaching_pairs(user, school, academic_year):
    """Every (section id, subject id) this person may set and check work for in a year."""
    employee = employee_of(user)
    if employee is None or academic_year is None:
        return set()
    return _pairs(school, academic_year, teacher_id=employee.pk)


def _pairs(school, academic_year, teacher_id=None):
    """Teaching assignments as (section id, subject id), with papers and whole subjects filled in."""
    from academics.models import Subject, SubjectTeacher

    rows = SubjectTeacher.objects.filter(school=school, academic_year=academic_year)
    if teacher_id is not None:
        rows = rows.filter(teacher_id=teacher_id)
    pairs = set()
    for section_id, subject_id, unit_id in rows.values_list("section_id", "subject_id", "subject__combines_into_id"):
        pairs.add((section_id, subject_id))
        if unit_id:
            pairs.add((section_id, unit_id))
    # Teaching a whole subject covers each of its papers.
    subject_ids = {subject_id for _section, subject_id in pairs}
    by_unit = {}
    for paper_id, unit_id in Subject.objects.filter(school=school, combines_into_id__in=subject_ids).values_list(
        "pk", "combines_into_id"
    ):
        by_unit.setdefault(unit_id, []).append(paper_id)
    for section_id, subject_id in list(pairs):
        for paper_id in by_unit.get(subject_id, []):
            pairs.add((section_id, paper_id))
    return pairs


def settable_units(user, school, academic_year):
    """
    What this person may set work for: [(class level, subject, [sections])], by class then
    subject. A manager may set work wherever the subject is taught; a teacher, where they teach.
    """
    from academics.models import Section, Subject

    if academic_year is None:
        return []
    pairs = _pairs(school, academic_year) if is_manager(user) else teaching_pairs(user, school, academic_year)
    sections = {
        s.pk: s
        for s in Section.objects.filter(school=school, pk__in={p[0] for p in pairs}, is_active=True).select_related(
            "class_level"
        )
    }
    subjects = Subject.objects.in_bulk({p[1] for p in pairs})
    units = {}
    for section_id, subject_id in pairs:
        section = sections.get(section_id)
        if section is None or subject_id not in subjects:
            continue
        units.setdefault((section.class_level, subjects[subject_id]), []).append(section)
    return [
        (level, subject, sorted(found, key=lambda s: s.name))
        for (level, subject), found in sorted(units.items(), key=lambda item: (item[0][0].order, item[0][1].name))
    ]


def is_class_teacher(user, section):
    employee = employee_of(user)
    return employee is not None and section.class_teacher_id == employee.pk


def may_set(user, school, subject, sections, academic_year):
    if is_manager(user):
        return True
    pairs = teaching_pairs(user, school, academic_year)
    return bool(sections) and all((section.pk, subject.pk) in pairs for section in sections)


def may_mark(user, task, section, pairs=None):
    """Check a section's work for a task: a teacher of that subject there, or a manager."""
    if is_manager(user):
        return True
    if pairs is None:
        pairs = teaching_pairs(user, task.school, task.academic_year)
    return (section.pk, task.subject_id) in pairs


def may_view(user, task, section, pairs=None):
    """Look at a section's work for a task: whoever may check it, and the section's class teacher."""
    return may_mark(user, task, section, pairs) or is_class_teacher(user, section)


def may_manage(user, task, pairs=None):
    """Edit, withdraw or delete a task: a manager, or a teacher of every section it is set for."""
    if is_manager(user):
        return True
    if task.status == task.Status.DRAFT and task.set_by_id == user.pk:
        return True
    if pairs is None:
        pairs = teaching_pairs(user, task.school, task.academic_year)
    sections = [target.section for target in task.targets.all()]
    return bool(sections) and all((section.pk, task.subject_id) in pairs for section in sections)


def visible_tasks(user, school):
    """The tasks a member of staff may open: every task for a manager, else their own teaching."""
    from django.db.models import Q

    from academics.models import AcademicYear

    from .models import Task

    tasks = Task.objects.filter(school=school)
    if is_manager(user):
        return tasks
    employee = employee_of(user)
    if employee is None:
        return tasks.none()
    year = AcademicYear.current_for(school)
    condition = Q(set_by=user)
    for section_id, subject_id in teaching_pairs(user, school, year):
        condition |= Q(academic_year=year, targets__section_id=section_id, subject_id=subject_id)
    condition |= Q(academic_year=year, targets__section__class_teacher=employee)
    # A draft is its author's until it is published; managers see every draft.
    return tasks.filter(condition).exclude(Q(status=Task.Status.DRAFT) & ~Q(set_by=user)).distinct()


def task_for_staff(user, school, pk):
    """A task this member of staff may open, or "not found"."""
    task = visible_tasks(user, school).filter(pk=pk).first()
    if task is None:
        raise Http404
    return task
