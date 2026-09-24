"""
The daily register: who may take it (a school setting), how long teachers may change it, and
who took it on a given day. A warning when a class has no subject plan.
"""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.utils import timezone

from attendance.models import StudentAttendance
from attendance.services import register_sections, save_register


def login(user):
    client = Client()
    client.force_login(user)
    return client


def last_school_day(school, before=None):
    from attendance.services import is_holiday

    day = before or timezone.localdate()
    while is_holiday(school, day):
        day -= timedelta(days=1)
    return day


def mark(erp, user, day):
    return save_register(school=erp.school, user=user, day=day, entries=[(erp.enrollment, "present", "", None, None)])


def test_by_default_any_teacher_of_the_section_can_take_the_register(erp):
    assert erp.section in register_sections(erp.teacher, erp.school)
    assert mark(erp, erp.teacher, last_school_day(erp.school)) == 1


def test_a_school_can_keep_the_register_to_the_class_teacher(erp):
    erp.school.register_takers = "class_teacher"
    erp.school.save()
    day = last_school_day(erp.school)
    with pytest.raises(PermissionDenied):
        mark(erp, erp.teacher, day)
    assert erp.section not in register_sections(erp.teacher, erp.school)
    erp.section.class_teacher = erp.employee
    erp.section.save()
    assert mark(erp, erp.teacher, day) == 1
    # Managers can always take it.
    assert mark(erp, erp.admin, day) == 1


def test_the_register_screen_offers_only_the_sections_a_person_may_take(erp):
    erp.school.register_takers = "class_teacher"
    erp.school.save()
    teacher = login(erp.teacher)
    before = teacher.get("/attendance/")
    assert before.status_code == 200
    assert f'value="{erp.section.pk}"' not in before.content.decode()
    erp.school.register_takers = "section_teachers"
    erp.school.save()
    assert f'value="{erp.section.pk}"' in teacher.get("/attendance/").content.decode()


def test_after_the_window_only_managers_change_a_register(erp):
    erp.school.register_edit_days = 3
    erp.school.save()
    old = last_school_day(erp.school, timezone.localdate() - timedelta(days=10))
    with pytest.raises(ValidationError) as caught:
        mark(erp, erp.teacher, old)
    assert "3 day(s)" in " ".join(caught.value.messages)
    assert mark(erp, erp.admin, old) == 1
    assert mark(erp, erp.teacher, last_school_day(erp.school)) == 1


def test_with_no_window_teachers_can_still_correct_old_registers(erp):
    old = last_school_day(erp.school, timezone.localdate() - timedelta(days=10))
    if old < erp.year.start_date:
        pytest.skip("the fixture's year has not run for ten days yet")
    assert mark(erp, erp.teacher, old) == 1


def test_the_daily_summary_names_who_took_each_register(erp):
    day = last_school_day(erp.school)
    mark(erp, erp.teacher, day)
    page = login(erp.admin).get(f"/attendance/summary/?date={day.isoformat()}").content.decode()
    assert "Taken by" in page and "Teacher" in page
    assert StudentAttendance.objects.get().recorded_by == erp.teacher


def test_a_class_without_a_subject_plan_is_flagged_before_publishing(erp, board):
    from academics.models import ClassSubject
    from examinations.checklist import publication_checklist

    ClassSubject.objects.filter(class_level=board.level).delete()
    texts = " ".join(item["text"] for item in publication_checklist(board.exam) if item["level"] == "warn")
    assert "has no subject plan" in texts


def test_a_teacher_with_one_section_opens_straight_onto_its_register(erp):
    body = login(erp.teacher).get("/attendance/").content.decode()
    assert "This field is required" not in body
    assert "Ayesha" in body  # the one section's register, already open
    # A link with just a date (the dashboard's) does the same.
    body = login(erp.teacher).get(f"/attendance/?date={timezone.localdate().isoformat()}").content.decode()
    assert "This field is required" not in body and "Ayesha" in body


def test_someone_with_several_sections_is_asked_to_choose_not_shown_an_error(erp):
    body = login(erp.admin).get("/attendance/").content.decode()
    assert "This field is required" not in body
    assert "Choose a section to open its register." in body
    assert "Ayesha" not in body
    report = login(erp.admin).get("/attendance/report/").content.decode()
    assert "This field is required" not in report
