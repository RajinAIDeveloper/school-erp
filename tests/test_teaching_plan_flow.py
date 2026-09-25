"""A class subject leads into optional teacher, routine and dated topic setup."""

from datetime import date

from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from academics.models import ClassSubject, TeachingPlanItem, Term
from academics.models import SubjectTeacher
from users.models import User


def _subject_row(erp):
    return ClassSubject.objects.create(
        school=erp.school,
        academic_year=erp.year,
        class_level=erp.level,
        subject=erp.subject,
        kind=ClassSubject.Kind.COMPULSORY,
    )


def test_subject_save_continues_to_optional_setup_with_prefilled_links(admin_client, erp):
    response = admin_client.post(reverse("settings:class_subject_create"), {
        "academic_year": erp.year.pk,
        "class_level": erp.level.pk,
        "subject": erp.subject.pk,
        "group": "",
        "kind": ClassSubject.Kind.COMPULSORY,
    })
    row = ClassSubject.objects.get(school=erp.school, class_level=erp.level, subject=erp.subject)
    assert response.status_code == 302
    assert response.url == reverse("academics:subject_setup", args=[row.pk])

    page = admin_client.get(response.url)
    assert page.status_code == 200
    assert b"Everything below is optional" in page.content
    assert b"Assign teacher" in page.content
    assert b"Set weekly routine" in page.content
    assert b"Future tests and quizzes" in page.content
    assert f"section={erp.section.pk}".encode() in page.content
    assert not TeachingPlanItem.objects.filter(school=erp.school).exists()

    other_section = next(s for s in page.context["section_rows"] if s["section"] == erp.other_section)
    assignment_form = admin_client.get(other_section["assign_url"])
    assert assignment_form.status_code == 200
    assert assignment_form.context["form"]["academic_year"].value() == str(erp.year.pk)
    assert assignment_form.context["form"]["section"].value() == str(erp.other_section.pk)
    assert assignment_form.context["form"]["subject"].value() == str(erp.subject.pk)
    assert assignment_form.context["cancel_url"] == response.url
    saved = admin_client.post(other_section["assign_url"], {
        "academic_year": erp.year.pk,
        "section": erp.other_section.pk,
        "subject": erp.subject.pk,
        "teacher": erp.employee.pk,
    })
    assert saved.status_code == 302 and saved.url == response.url
    assert SubjectTeacher.objects.filter(section=erp.other_section, subject=erp.subject, teacher=erp.employee).exists()


def test_school_can_plan_dated_topic_and_teacher_is_limited_to_assigned_section(admin_client, erp):
    row = _subject_row(erp)
    term = Term.objects.create(
        school=erp.school, academic_year=erp.year, name="Semester 1",
        start_date=date(2026, 1, 1), end_date=date(2026, 6, 30),
    )
    url = reverse("academics:subject_setup", args=[row.pk])
    response = admin_client.post(url, {
        "term": term.pk,
        "section": "",
        "planned_date": "2026-03-10",
        "kind": TeachingPlanItem.Kind.LESSON,
        "unit": "Fractions",
        "topic": "Adding fractions",
        "learning_goal": "Add fractions with different denominators",
    })
    assert response.status_code == 302
    shared = TeachingPlanItem.objects.get(school=erp.school, topic="Adding fractions")
    assert shared.section is None
    assert shared.term == term

    teacher_client = Client()
    teacher_client.force_login(erp.teacher)
    assert teacher_client.get(url).status_code == 200
    assert teacher_client.get(reverse("settings:class_subject_list")).status_code == 403
    assert teacher_client.get(reverse("academics:teaching_plan_edit", args=[shared.pk])).status_code == 403
    response = teacher_client.post(url, {
        "term": term.pk,
        "section": erp.other_section.pk,
        "planned_date": "2026-03-11",
        "kind": TeachingPlanItem.Kind.QUIZ,
        "topic": "Fractions quiz",
    })
    assert response.status_code == 200
    assert not TeachingPlanItem.objects.filter(topic="Fractions quiz").exists()
    response = teacher_client.post(url, {
        "term": term.pk,
        "section": erp.section.pk,
        "planned_date": "2026-03-11",
        "kind": TeachingPlanItem.Kind.QUIZ,
        "topic": "Fractions quiz",
    })
    assert response.status_code == 302
    assert TeachingPlanItem.objects.filter(section=erp.section, topic="Fractions quiz").exists()

    outsider = User.objects.create_user("unassigned-planner", school=erp.school, password="Test-pass-9842")
    outsider.groups.add(Group.objects.get(name="Teacher"))
    teacher_client.force_login(outsider)
    assert teacher_client.get(url).status_code == 403


def test_planning_date_must_fit_selected_term_and_other_schools_cannot_open_plan(admin_client, erp):
    row = _subject_row(erp)
    term = Term.objects.create(
        school=erp.school, academic_year=erp.year, name="Semester 1",
        start_date=date(2026, 1, 1), end_date=date(2026, 6, 30),
    )
    url = reverse("academics:subject_setup", args=[row.pk])
    response = admin_client.post(url, {
        "term": term.pk,
        "section": erp.section.pk,
        "planned_date": "2026-09-10",
        "kind": TeachingPlanItem.Kind.CLASS_TEST,
        "topic": "Term test",
    })
    assert response.status_code == 200
    assert b"inside the selected term" in response.content
    assert not TeachingPlanItem.objects.filter(topic="Term test").exists()

    other_admin = User.objects.create_user("other-planner", school=erp.other, password="Test-pass-9842")
    other_admin.groups.add(Group.objects.get(name="Administrator"))
    client = Client()
    client.force_login(other_admin)
    assert client.get(url).status_code == 404
