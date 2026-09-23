"""
Subject plans a school can start from, and copying a plan between years.

A subject plan is the curriculum as data: which subjects a class takes in a year, for which
group, and which are choices. Most schools change little from one year to the next, so the
commonest action is to copy last year's plan and adjust it. The one ready-made plan here is the
national curriculum's Classes 9 and 10, because it is the same across Bangla-medium schools.
English-medium schools' option blocks differ from school to school, so they start from a copy
or from scratch.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from core.access import assert_actor_school, assert_school
from core.models import AuditLog

from .models import ClassSubject, Subject

COMPULSORY = ClassSubject.Kind.COMPULSORY
CHOICE = ClassSubject.Kind.CHOICE

# The national curriculum's Classes 9-10 (the 2012 curriculum, restored from 2025): subject,
# board code, the subject it combines into, religion, group, kind. A starting plan to check
# against the board's current subject list and the school's own offer; codes and group
# composition are marked for verification in the documentation.
NATIONAL_9_10 = [
    ("Bangla 1st Paper", "101", "Bangla", "", "", COMPULSORY),
    ("Bangla 2nd Paper", "102", "Bangla", "", "", COMPULSORY),
    ("English 1st Paper", "107", "English", "", "", COMPULSORY),
    ("English 2nd Paper", "108", "English", "", "", COMPULSORY),
    ("General Mathematics", "109", "", "", "", COMPULSORY),
    ("Information and Communication Technology", "154", "", "", "", COMPULSORY),
    ("Islam and Moral Education", "111", "", "islam", "", COMPULSORY),
    ("Hindu Religion and Moral Education", "112", "", "hinduism", "", COMPULSORY),
    ("Buddhist Religion and Moral Education", "113", "", "buddhism", "", COMPULSORY),
    ("Christian Religion and Moral Education", "114", "", "christianity", "", COMPULSORY),
    ("Physics", "136", "", "", "science", COMPULSORY),
    ("Chemistry", "137", "", "", "science", COMPULSORY),
    ("Bangladesh and Global Studies", "150", "", "", "science", COMPULSORY),
    ("Biology", "138", "", "", "science", CHOICE),
    ("Higher Mathematics", "126", "", "", "science", CHOICE),
    ("Accounting", "146", "", "", "business", COMPULSORY),
    ("Finance and Banking", "152", "", "", "business", COMPULSORY),
    ("Business Entrepreneurship", "143", "", "", "business", COMPULSORY),
    ("Science", "127", "", "", "business", COMPULSORY),
    ("Geography and Environment", "110", "", "", "humanities", COMPULSORY),
    ("Civics and Citizenship", "140", "", "", "humanities", COMPULSORY),
    ("Bangladesh History and World Civilization", "153", "", "", "humanities", COMPULSORY),
    ("Science", "127", "", "", "humanities", COMPULSORY),
    ("Economics", "141", "", "", "humanities", CHOICE),
    ("Agriculture Studies", "134", "", "", "", CHOICE),
    ("Home Science", "151", "", "", "", CHOICE),
]


def _check(user, school, *objects):
    assert_actor_school(user, school)
    assert_school(school, *objects)
    if not user.has_perm("academics.add_classsubject"):
        raise PermissionDenied


@transaction.atomic
def load_national_plan(*, school, user, academic_year, class_level):
    """
    Create the national Classes 9-10 subjects (reusing any with the same code) and add them to
    one class's plan for one year. Rows already in the plan are left alone.
    """
    _check(user, school, academic_year, class_level)
    parents = {}
    added = 0
    for name, code, combines, religion, group, kind in NATIONAL_9_10:
        parent = None
        if combines:
            parent = parents.get(combines)
            if parent is None:
                parent, _ = Subject.objects.get_or_create(school=school, name=combines, defaults={"code": ""})
                parents[combines] = parent
        subject = Subject.objects.filter(school=school, code=code).first()
        if subject is None:
            subject = Subject.objects.filter(school=school, name=name).first()
        if subject is None:
            subject = Subject.objects.create(
                school=school, name=name, code=code, combines_into=parent, religion=religion
            )
        subject.class_levels.add(class_level)
        _, created = ClassSubject.objects.get_or_create(
            school=school,
            academic_year=academic_year,
            class_level=class_level,
            subject=subject,
            group=group,
            defaults={"kind": kind},
        )
        added += int(created)
    AuditLog.objects.create(
        school=school,
        user=user,
        action="subject_plan.preset_loaded",
        description=f"National Classes 9-10 plan: {added} row(s) added to {class_level} {academic_year}",
    )
    return added


@transaction.atomic
def copy_plan(*, school, user, from_year, to_year, class_level):
    """Copy one class's plan from one year to another, skipping rows the target already has."""
    _check(user, school, from_year, to_year, class_level)
    if from_year.pk == to_year.pk:
        raise ValidationError("Choose two different years.")
    rows = ClassSubject.objects.filter(school=school, academic_year=from_year, class_level=class_level)
    if not rows.exists():
        raise ValidationError(f"{class_level} has no subject plan in {from_year} to copy.")
    added = 0
    for row in rows:
        _, created = ClassSubject.objects.get_or_create(
            school=school,
            academic_year=to_year,
            class_level=class_level,
            subject=row.subject,
            group=row.group,
            defaults={"kind": row.kind},
        )
        added += int(created)
    AuditLog.objects.create(
        school=school,
        user=user,
        action="subject_plan.copied",
        description=f"{class_level}: {added} row(s) copied from {from_year} to {to_year}",
    )
    return added
