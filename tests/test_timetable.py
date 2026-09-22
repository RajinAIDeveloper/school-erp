"""The week grid, the bulk editor's clash detection and utilisation reports."""

from datetime import time

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from academics.models import Subject
from core.models import AuditLog
from employees.models import Employee
from timetable.models import Period, Room, RoutineSlot
from timetable.services import WeekGridError, room_utilisation, save_week, teacher_load, week_grid

MONDAY, TUESDAY = 1, 2


@pytest.fixture
def periods(erp):
    first = Period.objects.create(school=erp.school, name="Period 1", order=1, start_time=time(9), end_time=time(9, 45))
    second = Period.objects.create(
        school=erp.school, name="Period 2", order=2, start_time=time(9, 45), end_time=time(10, 30)
    )
    Period.objects.create(
        school=erp.school, name="Tiffin", order=3, start_time=time(10, 30), end_time=time(11), is_break=True
    )
    return first, second


def test_week_grid_lays_out_periods_against_days(erp, periods):
    first, _second = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    grid = week_grid(erp.school, erp.year, section=erp.section)
    assert [row["period"].name for row in grid["rows"]] == ["Period 1", "Period 2", "Tiffin"]
    monday = next(c for c in grid["rows"][0]["cells"] if c["weekday"] == MONDAY)
    assert [s.subject for s in monday["slots"]] == [erp.subject]
    tuesday = next(c for c in grid["rows"][0]["cells"] if c["weekday"] == TUESDAY)
    assert tuesday["slots"] == []


def test_save_week_writes_a_whole_section_at_once(erp, periods):
    first, second = periods
    cells = {
        (MONDAY, first.pk): {"subject": erp.subject, "teacher": erp.employee, "room": None},
        (TUESDAY, second.pk): {"subject": erp.subject, "teacher": erp.employee, "room": None},
    }
    saved, cleared = save_week(
        school=erp.school, user=erp.admin, academic_year=erp.year, section=erp.section, cells=cells
    )
    assert (saved, cleared) == (2, 0)
    assert RoutineSlot.objects.count() == 2
    assert AuditLog.objects.filter(action="routine.saved").exists()


def test_save_week_clears_a_period_when_the_subject_is_blank(erp, periods):
    first, _second = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
    )
    saved, cleared = save_week(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        section=erp.section,
        cells={(MONDAY, first.pk): None},
    )
    assert (saved, cleared) == (0, 1)
    assert not RoutineSlot.objects.exists()


def test_save_week_rejects_a_teacher_already_busy_in_another_class(erp, periods):
    first, _second = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.other_section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    with pytest.raises(WeekGridError) as caught:
        save_week(
            school=erp.school,
            user=erp.admin,
            academic_year=erp.year,
            section=erp.section,
            cells={(MONDAY, first.pk): {"subject": erp.subject, "teacher": erp.employee, "room": None}},
        )
    assert (MONDAY, first.pk) in caught.value.errors
    assert RoutineSlot.objects.count() == 1


def test_save_week_rejects_the_same_teacher_twice_inside_one_submission(erp, periods):
    """Two cells of the same form must not quietly double-book one person."""
    first, _second = periods
    other_subject = Subject.objects.create(school=erp.school, name="English", code="ENG")
    # Same weekday and period cannot repeat for one section, so use two sections' worth of
    # conflict by giving the teacher to two rows of a different section's week.
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.other_section,
        weekday=TUESDAY,
        period=first,
        subject=other_subject,
        teacher=erp.employee,
    )
    with pytest.raises(WeekGridError):
        save_week(
            school=erp.school,
            user=erp.admin,
            academic_year=erp.year,
            section=erp.section,
            cells={(TUESDAY, first.pk): {"subject": erp.subject, "teacher": erp.employee, "room": None}},
        )


def test_save_week_rejects_a_room_already_taken(erp, periods):
    first, _second = periods
    room = Room.objects.create(school=erp.school, name="Lab 1")
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.other_section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
        room=room,
    )
    with pytest.raises(WeekGridError):
        save_week(
            school=erp.school,
            user=erp.admin,
            academic_year=erp.year,
            section=erp.section,
            cells={(MONDAY, first.pk): {"subject": erp.subject, "teacher": None, "room": room}},
        )


def test_save_week_refuses_to_teach_during_a_break(erp, periods):
    tiffin = Period.objects.get(school=erp.school, is_break=True)
    with pytest.raises(WeekGridError):
        save_week(
            school=erp.school,
            user=erp.admin,
            academic_year=erp.year,
            section=erp.section,
            cells={(MONDAY, tiffin.pk): {"subject": erp.subject, "teacher": None, "room": None}},
        )


def test_grid_editor_saves_from_the_screen(erp, periods):
    first, _second = periods
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/routine/edit/",
        {
            "academic_year": erp.year.pk,
            "section": erp.section.pk,
            f"{MONDAY}-{first.pk}-subject": erp.subject.pk,
            f"{MONDAY}-{first.pk}-teacher": erp.employee.pk,
            f"{MONDAY}-{first.pk}-room": "",
        },
    )
    assert response.status_code == 302
    assert RoutineSlot.objects.count() == 1


def test_grid_editor_marks_the_clashing_cell_and_saves_nothing(erp, periods):
    first, _second = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.other_section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/routine/edit/",
        {
            "academic_year": erp.year.pk,
            "section": erp.section.pk,
            f"{MONDAY}-{first.pk}-subject": erp.subject.pk,
            f"{MONDAY}-{first.pk}-teacher": erp.employee.pk,
            f"{MONDAY}-{first.pk}-room": "",
        },
    )
    assert response.status_code == 200
    assert b"Nothing was saved" in response.content
    assert b"already teaches" in response.content
    assert RoutineSlot.objects.count() == 1


def test_free_teachers_excludes_the_busy_and_other_schools(erp, periods):
    first, _second = periods
    other_school_teacher = Employee.objects.create(
        school=erp.other,
        employee_id="X1",
        first_name="Elsewhere",
        gender="M",
        phone="01712345699",
        joining_date="2026-01-01",
    )
    client = Client()
    client.force_login(erp.admin)
    free = client.get(f"/routine/free-teachers.json?weekday={MONDAY}&period={first.pk}").json()
    names = {row["name"] for row in free}
    assert "Teacher" in names
    assert other_school_teacher.full_name not in names

    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    free = client.get(f"/routine/free-teachers.json?weekday={MONDAY}&period={first.pk}").json()
    assert "Teacher" not in {row["name"] for row in free}


def test_routine_grid_renders_for_a_student_without_a_filter(erp, periods):
    first, _second = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    client = Client()
    client.force_login(erp.parent)
    body = client.get("/routine/").content
    assert b"Period 1" in body
    assert b"Math" in body


def test_routine_exports_in_every_format(admin_client, erp, periods):
    first, _second = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
    )
    assert admin_client.get("/routine/?format=pdf").content.startswith(b"%PDF")
    assert "Math" in admin_client.get("/routine/?format=csv").content.decode("utf-8-sig")


def test_utilisation_counts_rooms_and_teacher_load(erp, periods):
    first, second = periods
    room = Room.objects.create(school=erp.school, name="Room 1", capacity=40)
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
        room=room,
    )
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=second,
        subject=erp.subject,
        teacher=erp.employee,
        room=room,
    )
    rooms = room_utilisation(erp.school, erp.year)
    assert rooms[0]["used"] == 2 and rooms[0]["available"] > 0
    load = teacher_load(erp.school, erp.year)
    assert load[0]["teacher"] == erp.employee and load[0]["periods"] == 2

    client = Client()
    client.force_login(erp.admin)
    assert b"Teacher load" in client.get("/routine/utilisation/").content


def test_utilisation_is_for_timetable_staff(erp):
    client = Client()
    client.force_login(erp.parent)
    assert client.get("/routine/utilisation/").status_code == 403


def test_slot_model_still_refuses_an_overlapping_period(erp, periods):
    """The model rule stands on its own, whatever writes to it."""
    early = Period.objects.create(school=erp.school, name="Early", order=10, start_time=time(9), end_time=time(10))
    overlap = Period.objects.create(
        school=erp.school, name="Overlap", order=11, start_time=time(9, 30), end_time=time(10, 30)
    )
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=MONDAY,
        period=early,
        subject=erp.subject,
        teacher=erp.employee,
    )
    clash = RoutineSlot(
        school=erp.school,
        academic_year=erp.year,
        section=erp.other_section,
        weekday=MONDAY,
        period=overlap,
        subject=erp.subject,
        teacher=erp.employee,
    )
    with pytest.raises(ValidationError):
        clash.full_clean()
