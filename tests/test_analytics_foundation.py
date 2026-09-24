"""
Phase 6a, the ground analytics stand on.

- A Vice Principal has exactly the Principal's access.
- Only the platform administrator sees the platform page and switches a school's modules; a
  school without a module meets "not found".
- The analytics tables hold exactly what was published, replaced whenever a result is
  republished, and can be rebuilt from the snapshots.
- One access check decides what each person may analyse, never beyond their own school.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.http import Http404
from django.test import Client, RequestFactory

from academics.models import AcademicYear, ClassLevel, Section, SubjectTeacher
from analytics.access import scope_for
from core.models import AuditLog
from core.modules import has_module, require_module
from examinations.models import ExamResultFact, ResultSnapshot, SubjectResultFact, UnlockRequest
from examinations.services import publish_exam, review_unlock, save_marks
from students.models import Enrollment, Student
from tests.test_board_results import mark_everything
from users.models import User


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def platform_admin(db):
    return User.objects.create_superuser(username="platform", password="Test-pass-9842", email="p@example.com")


# ------------------------------------------------------------------ Vice Principal


def test_a_vice_principal_has_exactly_the_principals_access(erp):
    principal = set(Group.objects.get(name="Principal").permissions.values_list("codename", "content_type"))
    vice = set(Group.objects.get(name="Vice Principal").permissions.values_list("codename", "content_type"))
    assert vice == principal and vice
    user = User.objects.create_user(username="vice", school=erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name="Vice Principal"))
    from core.access import is_manager

    assert is_manager(user)
    assert scope_for(user, erp.school).whole_school
    # The manager's dashboard, as a principal gets.
    assert "Admit a student" in login(user).get("/").content.decode()


# ------------------------------------------------------------------ the platform page and modules


def test_only_the_platform_administrator_sees_the_platform_page(erp, platform_admin):
    page = login(platform_admin).get("/platform/").content.decode()
    assert "Test School" in page and "Other School" in page and "Homework" in page
    for user in (erp.admin, erp.teacher, erp.parent):
        assert login(user).get("/platform/").status_code == 404


def test_the_platform_administrator_gives_and_takes_a_module(erp, platform_admin):
    client = login(platform_admin)
    assert not has_module(erp.school, "homework")
    client.post("/platform/", {"school": erp.school.pk, "module": "homework", "action": "enable"})
    erp.school.refresh_from_db()
    erp.other.refresh_from_db()
    assert has_module(erp.school, "homework") and not has_module(erp.other, "homework")
    assert AuditLog.objects.filter(school=erp.school, action="platform.module_enabled").exists()
    client.post("/platform/", {"school": erp.school.pk, "module": "homework", "action": "disable"})
    erp.school.refresh_from_db()
    assert not has_module(erp.school, "homework")
    # A school's own administrator cannot switch it, even by posting directly.
    assert (
        login(erp.admin)
        .post("/platform/", {"school": erp.school.pk, "module": "homework", "action": "enable"})
        .status_code
        == 404
    )
    erp.school.refresh_from_db()
    assert not erp.school.homework_enabled


def test_a_school_without_a_module_gets_not_found(erp):
    @require_module("homework")
    def homework_view(request):
        return "shown"

    request = RequestFactory().get("/homework/")
    request.school = erp.school
    with pytest.raises(Http404):
        homework_view(request)
    erp.school.homework_enabled = True
    assert homework_view(request) == "shown"


def test_no_school_settings_form_offers_the_module_switches():
    from django import forms as django_forms

    import core.forms

    for form in vars(core.forms).values():
        if isinstance(form, type) and issubclass(form, django_forms.ModelForm) and form is not django_forms.ModelForm:
            fields = form._meta.fields or []
            assert "homework_enabled" not in fields and "admissions_enabled" not in fields, form


def test_the_platform_administrator_can_work_in_any_school(erp, platform_admin):
    client = login(platform_admin)
    client.post("/platform/", {"school": erp.other.pk, "action": "work_in"})
    assert "Other School" in client.get("/").content.decode()


# ------------------------------------------------------------------ the analytics tables


def test_publishing_writes_exactly_what_the_cards_print(erp, board):
    mark_everything(erp, board, {(board.science.pk, "136"): 20})  # Nabila fails Physics
    publish_exam(board.exam, erp.admin)
    facts = {f.enrollment_id: f for f in ExamResultFact.objects.filter(exam=board.exam)}
    assert len(facts) == 2
    for snapshot in ResultSnapshot.objects.filter(exam=board.exam):
        fact, payload = facts[snapshot.enrollment_id], snapshot.payload
        assert fact.percent == Decimal(payload["percent"]) and fact.version == 1
        assert fact.gpa == (Decimal(payload["gpa"]) if payload["gpa"] is not None else None)
        assert fact.result == payload["result"] and fact.section_rank == payload["rank"]
        assert fact.subjects.count() == len(payload["subjects"])
    nabila = facts[board.science.pk]
    assert nabila.subjects_failed == 1
    bangla = nabila.subjects.get(subject=board.subjects.bangla)
    assert bangla.papers == 2 and bangla.schedule_id is None
    physics = nabila.subjects.get(subject=board.subjects.physics)
    assert physics.score == Decimal(20) and physics.passed is False and physics.schedule_id is not None


def test_a_correction_replaces_the_analytics_rows(erp):
    save_marks(
        user=erp.teacher, schedule=erp.schedule, section=erp.section, rows=[(erp.enrollment, Decimal(60), False, 0)]
    )
    publish_exam(erp.exam, erp.admin)
    unlock = UnlockRequest.objects.create(
        school=erp.school, schedule=erp.schedule, requested_by=erp.teacher, reason="Re-totalled"
    )
    review_unlock(unlock, erp.admin, True)
    save_marks(
        user=erp.teacher, schedule=erp.schedule, section=erp.section, rows=[(erp.enrollment, Decimal(75), False, 1)]
    )
    fact = ExamResultFact.objects.get(exam=erp.exam)
    assert fact.version == 2 and fact.total == Decimal(75)
    assert SubjectResultFact.objects.filter(exam=erp.exam).count() == 1


def test_the_tables_can_be_rebuilt_from_the_snapshots(erp, board):
    mark_everything(erp, board)
    publish_exam(board.exam, erp.admin)
    before = list(ExamResultFact.objects.order_by("enrollment_id").values_list("enrollment_id", "percent", "gpa"))
    subjects = SubjectResultFact.objects.count()
    ExamResultFact.objects.all().delete()
    call_command("rebuild_result_facts", verbosity=0)
    assert (
        list(ExamResultFact.objects.order_by("enrollment_id").values_list("enrollment_id", "percent", "gpa")) == before
    )
    assert SubjectResultFact.objects.count() == subjects


# ------------------------------------------------------------------ who may analyse what


def add_user(erp, username, role, school=None):
    user = User.objects.create_user(username=username, school=school or erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name=role))
    return user


def test_each_role_sees_its_own_part_of_the_school(erp, board):
    mark_everything(erp, board)
    publish_exam(board.exam, erp.admin)
    everything = ExamResultFact.objects.all()

    # Managers: the whole school.
    for user in (erp.admin, add_user(erp, "principal", "Principal"), add_user(erp, "vice", "Vice Principal")):
        scope = scope_for(user, erp.school)
        assert scope.whole_school and scope.results(everything).count() == 2

    # A subject teacher of Bangla 1st Paper sees Bangla (both papers) in that section, nothing else.
    b1 = board.schedules["101"].subject
    SubjectTeacher.objects.filter(teacher=erp.employee, section=board.section).exclude(subject=b1).delete()
    scope = scope_for(erp.teacher, erp.school)
    seen = scope.subject_results(SubjectResultFact.objects.all())
    assert set(seen.values_list("subject", flat=True)) == {board.subjects.bangla.pk}
    assert scope.results(everything).count() == 0  # overall results belong to the class teacher

    # As class teacher of the section, the same teacher sees it in full.
    board.section.class_teacher = erp.employee
    board.section.save()
    scope = scope_for(erp.teacher, erp.school)
    assert scope.results(everything).count() == 2
    assert scope.subject_results(SubjectResultFact.objects.all()).count() == SubjectResultFact.objects.count()

    # Accountants and staff see nothing.
    assert scope_for(erp.accountant, erp.school).empty
    assert scope_for(erp.staff, erp.school).empty


def test_a_family_sees_only_its_own_children(erp, board):
    from students.models import StudentGuardian

    mark_everything(erp, board)
    publish_exam(board.exam, erp.admin)
    StudentGuardian.objects.create(student=board.science.student, guardian=erp.guardian, relation="mother")
    scope = scope_for(erp.parent, erp.school)
    assert scope.family
    seen = scope.results(ExamResultFact.objects.all())
    assert list(seen.values_list("enrollment_id", flat=True)) == [board.science.pk]
    assert not scope.results(ExamResultFact.objects.filter(enrollment=board.humanities)).exists()


def test_nobody_reaches_another_schools_results(erp, board):
    mark_everything(erp, board)
    publish_exam(board.exam, erp.admin)
    outsider = add_user(erp, "outsider", "Administrator", school=erp.other)
    assert scope_for(outsider, erp.school).empty
    assert scope_for(outsider, erp.other).results(ExamResultFact.objects.all()).count() == 0


def test_a_teachers_access_is_for_the_year_they_teach(erp, board):
    from students.models import StudentGuardian

    mark_everything(erp, board)
    publish_exam(board.exam, erp.admin)
    board.section.class_teacher = erp.employee
    board.section.save()
    StudentGuardian.objects.create(student=board.science.student, guardian=erp.guardian, relation="mother")
    # A new year: the same section, but last year's results are not this year's class teacher's.
    erp.year.is_current = False
    erp.year.save()
    AcademicYear.objects.create(
        school=erp.school, name="2027", start_date=date(2027, 1, 1), end_date=date(2027, 12, 31), is_current=True
    )
    assert scope_for(erp.teacher, erp.school).results(ExamResultFact.objects.all()).count() == 0
    # A family keeps every year of its own child.
    assert scope_for(erp.parent, erp.school).results(ExamResultFact.objects.all()).count() == 1
