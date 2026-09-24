"""
Teacher assignments: a subject may have more than one teacher in a section, and an
assignment must name a teacher and a subject the class actually takes.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from academics.models import ClassLevel, ClassSubject, Subject, SubjectTeacher
from employees.models import Employee
from examinations.services import assert_can_mark, save_mark


def employee(erp, employee_id, kind="teacher"):
    return Employee.objects.create(
        school=erp.school,
        employee_id=employee_id,
        first_name=employee_id,
        gender="F",
        joining_date=date(2025, 1, 1),
        phone="01712345670",
        employee_type=kind,
    )


def assign(erp, teacher, subject=None):
    return SubjectTeacher(
        school=erp.school, academic_year=erp.year, section=erp.section, subject=subject or erp.subject, teacher=teacher
    )


def test_two_teachers_can_share_a_subject_in_a_section(erp):
    from django.contrib.auth.models import Group

    from users.models import User

    partner = employee(erp, "T2")
    partner.user = User.objects.create_user(username="partner", school=erp.school, password="Test-pass-9842")
    partner.user.groups.add(Group.objects.get(name="Teacher"))
    partner.save()
    row = assign(erp, partner)
    row.full_clean()
    row.save()
    assert SubjectTeacher.objects.filter(section=erp.section, subject=erp.subject).count() == 2
    for user in (erp.teacher, partner.user):
        assert_can_mark(user, erp.schedule, section=erp.section)
    save_mark(user=partner.user, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(60))


def test_the_same_teacher_is_not_assigned_twice(erp):
    with pytest.raises(ValidationError):
        assign(erp, erp.employee).full_clean()


def test_office_staff_cannot_be_assigned_a_subject(erp):
    clerk = employee(erp, "S9", kind="staff")
    with pytest.raises(ValidationError) as caught:
        assign(erp, clerk).full_clean()
    assert "teacher" in caught.value.message_dict


def test_a_subject_the_class_does_not_take_is_refused(erp):
    other_class = ClassLevel.objects.create(school=erp.school, name="Class 8", order=8)
    art = Subject.objects.create(school=erp.school, name="Art", code="ART")
    art.class_levels.add(other_class)
    with pytest.raises(ValidationError) as caught:
        assign(erp, employee(erp, "T3"), art).full_clean()
    assert "not taught in Class 1" in " ".join(caught.value.message_dict["subject"])


def test_a_subject_outside_the_years_plan_is_refused(erp):
    music = Subject.objects.create(school=erp.school, name="Music", code="MUS")
    ClassSubject.objects.create(
        school=erp.school, academic_year=erp.year, class_level=erp.level, subject=erp.subject, kind="compulsory"
    )
    with pytest.raises(ValidationError) as caught:
        assign(erp, employee(erp, "T4"), music).full_clean()
    assert "subject plan" in " ".join(caught.value.message_dict["subject"])


def test_the_settings_form_shows_the_problem(erp):
    clerk = employee(erp, "S8", kind="staff")
    client = Client()
    client.force_login(erp.admin)
    page = client.post(
        "/settings/subject-teacher/new/",
        {"academic_year": erp.year.pk, "section": erp.section.pk, "subject": erp.subject.pk, "teacher": clerk.pk},
    )
    assert page.status_code == 200 and "not the teaching staff" in page.content.decode()
