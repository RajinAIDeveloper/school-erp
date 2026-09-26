"""A section classroom fills ordinary lessons; explicit and dated rooms override it."""

from datetime import date, time

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from timetable.models import Period, Room, RoutineSlot, SectionRoomChange
from timetable.services import room_utilisation, week_grid


@pytest.fixture
def classrooms(erp):
    home = Room.objects.create(school=erp.school, name="Class 7 Rose")
    other = Room.objects.create(school=erp.school, name="Spare classroom")
    gym = Room.objects.create(school=erp.school, name="Gym")
    period = Period.objects.create(
        school=erp.school, name="1st period", order=1, start_time=time(9), end_time=time(9, 45)
    )
    erp.section.default_room = home
    erp.section.save(update_fields=["default_room"])
    return home, other, gym, period


def _slot(erp, period, *, section=None, room=None, weekday=1):
    return RoutineSlot.objects.create(
        school=erp.school, academic_year=erp.year, section=section or erp.section,
        weekday=weekday, period=period, subject=erp.subject, room=room,
    )


def test_section_form_and_weekly_grid_offer_its_default_classroom(admin_client, erp, classrooms):
    home, _other, _gym, period = classrooms
    foreign = Room.objects.create(school=erp.other, name="Other school's room")
    form = admin_client.get(reverse("settings:section_update", args=[erp.section.pk])).context["form"]
    assert form["default_room"].value() == home.pk
    assert foreign not in form.fields["default_room"].queryset

    editor = admin_client.get(f"/routine/edit/?academic_year={erp.year.pk}&section={erp.section.pk}")
    assert f"Section classroom: {home.name}".encode() in editor.content
    saved = admin_client.post("/routine/edit/", {
        "academic_year": erp.year.pk, "section": erp.section.pk,
        f"1-{period.pk}-subject": erp.subject.pk,
        f"1-{period.pk}-teacher": "", f"1-{period.pk}-room": "",
    })
    assert saved.status_code == 302
    slot = RoutineSlot.objects.get(section=erp.section, period=period)
    assert slot.room is None and slot.regular_room == home
    assert home.name.encode() in admin_client.get(f"/routine/?section={erp.section.pk}").content
    assert next(row for row in room_utilisation(erp.school, erp.year) if row["room"] == home)["used"] == 1


def test_temporary_change_moves_classroom_for_dates_but_keeps_gym(erp, classrooms):
    home, spare, gym, period = classrooms
    ordinary = _slot(erp, period, weekday=1)
    gym_period = Period.objects.create(
        school=erp.school, name="2nd period", order=2, start_time=time(9, 45), end_time=time(10, 30)
    )
    gym_lesson = _slot(erp, gym_period, room=gym, weekday=1)
    change = SectionRoomChange.objects.create(
        school=erp.school, section=erp.section, room=spare,
        start_date=date(2026, 9, 28), end_date=date(2026, 9, 29), reason="Maintenance",
    )
    change.full_clean()

    regular = week_grid(erp.school, erp.year, section=erp.section)
    dated = week_grid(erp.school, erp.year, section=erp.section, week_of=date(2026, 9, 28))
    regular_rooms = {slot.pk: slot.display_room for row in regular["rows"] for cell in row["cells"] for slot in cell["slots"]}
    dated_rooms = {slot.pk: slot.display_room for row in dated["rows"] for cell in row["cells"] for slot in cell["slots"]}
    assert regular_rooms[ordinary.pk] == home
    assert dated_rooms[ordinary.pk] == spare
    assert dated_rooms[gym_lesson.pk] == gym

    client = Client()
    client.force_login(erp.admin)
    body = client.get(f"/routine/?section={erp.section.pk}&week_of=2026-09-28").content.decode()
    assert spare.name in body and gym.name in body
    assert spare.name in client.get(f"/routine/?section={erp.section.pk}&week_of=2026-09-28&format=csv").content.decode()


def test_default_and_temporary_classrooms_refuse_double_booking(erp, classrooms):
    home, spare, _gym, period = classrooms
    erp.other_section.default_room = home
    erp.other_section.save(update_fields=["default_room"])
    _slot(erp, period, section=erp.other_section)
    with pytest.raises(ValidationError, match="used by"):
        RoutineSlot(
            school=erp.school, academic_year=erp.year, section=erp.section,
            weekday=1, period=period, subject=erp.subject,
        ).full_clean(exclude=["school"])

    erp.other_section.default_room = spare
    erp.other_section.save(update_fields=["default_room"])
    _slot(erp, period, section=erp.section)
    with pytest.raises(ValidationError, match="used by"):
        SectionRoomChange(
            school=erp.school, section=erp.section, room=spare,
            start_date=date(2026, 9, 28), end_date=date(2026, 9, 28),
        ).full_clean()


def test_changing_a_sections_classroom_checks_its_existing_lessons(erp, classrooms):
    _home, spare, _gym, period = classrooms
    erp.other_section.default_room = spare
    erp.other_section.save(update_fields=["default_room"])
    _slot(erp, period, section=erp.other_section)
    _slot(erp, period, section=erp.section)
    erp.section.default_room = spare
    with pytest.raises(ValidationError, match="already used"):
        erp.section.full_clean()


def test_new_recurring_lesson_checks_existing_temporary_room_move(erp, classrooms):
    _home, spare, _gym, period = classrooms
    erp.other_section.default_room = spare
    erp.other_section.save(update_fields=["default_room"])
    _slot(erp, period, section=erp.section)
    SectionRoomChange.objects.create(
        school=erp.school, section=erp.section, room=spare,
        start_date=date(2026, 9, 28), end_date=date(2026, 9, 28),
    )
    with pytest.raises(ValidationError, match="28 Sep 2026"):
        RoutineSlot(
            school=erp.school, academic_year=erp.year, section=erp.other_section,
            weekday=1, period=period, subject=erp.subject,
        ).full_clean(exclude=["school"])


def test_managers_can_create_and_edit_dated_room_change(admin_client, erp, classrooms):
    _home, spare, gym, _period = classrooms
    create = reverse("timetable:room_change_create")
    response = admin_client.post(create, {
        "section": erp.section.pk, "room": spare.pk,
        "start_date": "2026-09-28", "end_date": "2026-10-02", "reason": "Maintenance",
    })
    assert response.status_code == 302
    change = SectionRoomChange.objects.get(section=erp.section)
    response = admin_client.post(reverse("timetable:room_change_update", args=[change.pk]), {
        "section": erp.section.pk, "room": gym.pk,
        "start_date": "2026-09-28", "end_date": "2026-09-28", "reason": "One day",
    })
    assert response.status_code == 302
    change.refresh_from_db()
    assert change.room == gym and change.end_date == date(2026, 9, 28)

    client = Client()
    client.force_login(erp.teacher)
    assert client.get(reverse("timetable:room_change_list")).status_code == 403
