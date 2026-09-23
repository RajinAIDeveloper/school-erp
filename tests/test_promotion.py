"""
Promotion from results: advice from a published result, a tick per student, and a recorded
reason whenever the school goes against the advice.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from academics.models import AcademicYear, ClassLevel, Section
from core.models import AuditLog
from examinations.advice import advise, basis_choices, published_rows
from examinations.services import publish_exam, save_mark
from students.models import Enrollment, Student
from students.services import promote_from_results


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def year_end(erp):
    """Two students in Class 1 A: Ayesha passes, Rafi fails. Next year's Class 2 is ready."""
    rafi = Student.objects.create(
        school=erp.school,
        student_id="S2",
        first_name="Rafi",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    failing = Enrollment.objects.create(
        school=erp.school,
        student=rafi,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=failing, score=Decimal(10))
    publish_exam(erp.exam, erp.admin)
    next_year = AcademicYear.objects.create(
        school=erp.school, name="2027", start_date=date(2027, 1, 1), end_date=date(2027, 12, 31)
    )
    class_two = ClassLevel.objects.create(school=erp.school, name="Class 2", order=2)
    target = Section.objects.create(school=erp.school, class_level=class_two, name="A")
    from types import SimpleNamespace

    return SimpleNamespace(failing=failing, next_year=next_year, target=target, basis=f"exam-{erp.exam.pk}")


def decide(erp, ye, overrides=None):
    results, _ = published_rows(erp.school, ye.basis)
    decisions = {}
    for e in (erp.enrollment, ye.failing):
        advised, _why = advise(results.get(e.pk))
        decisions[e.pk] = {"promote": advised, "advised": advised, "reason": ""}
    for pk, change in (overrides or {}).items():
        decisions[pk].update(change)
    return decisions


def run(erp, ye, decisions, repeat=None):
    return promote_from_results(
        school=erp.school,
        user=erp.admin,
        source_year=erp.year,
        source_section=erp.section,
        target_year=ye.next_year,
        target_section=ye.target,
        decisions=decisions,
        repeat_section=repeat,
        basis="Term 1",
    )


def test_advice_follows_the_published_result(erp, year_end):
    results, name = published_rows(erp.school, year_end.basis)
    assert name == "Term 1"
    assert advise(results[erp.enrollment.pk])[0] is True
    promote, why = advise(results[year_end.failing.pk])
    assert promote is False and why.startswith("Failed")
    assert advise(None) == (False, "No published result")
    assert (year_end.basis, "Exam: Term 1") in basis_choices(erp.school, erp.year)


def test_a_grade_only_result_advises_promotion_and_says_to_check_policy():
    promote, why = advise({"result": None, "complete": True, "headline": "2 A*", "rulebook": "Cambridge grades"})
    assert promote and "no pass or fail under Cambridge grades" in why


def test_following_the_advice_promotes_passes_and_re_enrols_the_rest(erp, year_end):
    assert run(erp, year_end, decide(erp, year_end), repeat=erp.section) == (1, 1)
    erp.enrollment.refresh_from_db()
    year_end.failing.refresh_from_db()
    assert erp.enrollment.status == "promoted" and year_end.failing.status == "repeated"
    assert Enrollment.objects.get(student=erp.student, academic_year=year_end.next_year).section == year_end.target
    assert (
        Enrollment.objects.get(student=year_end.failing.student, academic_year=year_end.next_year).section
        == erp.section
    )


def test_holding_back_without_re_enrolling_leaves_the_student_where_they_are(erp, year_end):
    run(erp, year_end, decide(erp, year_end))
    year_end.failing.refresh_from_db()
    assert year_end.failing.status == "enrolled"
    assert not Enrollment.objects.filter(student=year_end.failing.student, academic_year=year_end.next_year).exists()


def test_going_against_the_advice_needs_a_reason_and_is_recorded(erp, year_end):
    against = {year_end.failing.pk: {"promote": True}}
    with pytest.raises(ValidationError) as caught:
        run(erp, year_end, decide(erp, year_end, against))
    assert "Rafi" in " ".join(caught.value.messages)
    assert not Enrollment.objects.filter(academic_year=year_end.next_year).exists()
    against[year_end.failing.pk]["reason"] = "Passed the re-sit in December"
    assert run(erp, year_end, decide(erp, year_end, against)) == (2, 0)
    log = AuditLog.objects.get(action="students.promotion_override")
    assert "Rafi" in log.description and "Passed the re-sit in December" in log.description


def test_a_stale_list_is_refused(erp, year_end):
    decisions = decide(erp, year_end)
    decisions.pop(year_end.failing.pk)
    with pytest.raises(ValidationError):
        run(erp, year_end, decisions)


def test_a_repeat_section_must_be_the_same_class(erp, year_end):
    with pytest.raises(ValidationError):
        run(erp, year_end, decide(erp, year_end), repeat=year_end.target)


def test_only_staff_who_manage_enrolments_promote(erp, year_end):
    with pytest.raises(PermissionDenied):
        promote_from_results(
            school=erp.school,
            user=erp.teacher,
            source_year=erp.year,
            source_section=erp.section,
            target_year=year_end.next_year,
            target_section=year_end.target,
            decisions=decide(erp, year_end),
        )
    assert login(erp.teacher).get("/students/promote/").status_code == 403


def test_the_promotion_screen_lists_advice_and_promotes(erp, year_end):
    client = login(erp.admin)
    fields = {
        "source_year": erp.year.pk,
        "source_section": erp.section.pk,
        "target_year": year_end.next_year.pk,
        "target_section": year_end.target.pk,
        "basis": year_end.basis,
        "repeat_section": erp.section.pk,
    }
    page = client.get("/students/promote/", fields).content.decode()
    assert "Advice from Term 1" in page and "Failed" in page and "Passed" in page
    response = client.post(
        "/students/promote/",
        {**fields, f"{erp.enrollment.pk}-promote": "on", f"{year_end.failing.pk}-reason": ""},
    )
    assert response.status_code == 302
    assert Enrollment.objects.filter(academic_year=year_end.next_year).count() == 2
    year_end.failing.refresh_from_db()
    assert year_end.failing.status == "repeated"
