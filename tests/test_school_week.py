"""
The school week: which days the school meets, the periods of the day, days with other
timings, and a routine that follows all three.
"""

from datetime import date, time

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from academics.models import Section
from core.models import AuditLog
from timetable.models import Period, PeriodDayTime, RoutineSlot
from timetable.services import build_day, free_teachers, plan_day, room_utilisation, save_day_times, stray_slots

THURSDAY, FRIDAY, SUNDAY = 4, 5, 7


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def day(erp):
    """A 3-period day built the quick way: 08:00, 45 minutes each, a break after the 2nd."""
    return build_day(school=erp.school, user=erp.admin, plan=plan_day(time(8), 45, 3, [(2, 30, "Tiffin")]))


def slot(erp, period, weekday, section=None, teacher=None):
    return RoutineSlot(
        school=erp.school,
        academic_year=erp.year,
        section=section or erp.section,
        weekday=weekday,
        period=period,
        subject=erp.subject,
        teacher=teacher,
    )


# ------------------------------------------------------------------ school days


def test_school_days_are_ticked_on_the_school_profile(erp):
    client = login(erp.admin)
    page = client.get("/settings/school/").content.decode()
    assert "School days" in page and 'name="weekend_days"' not in page
    form = {
        "name": "Test School",
        "currency": "BDT",
        "currency_symbol": "Tk",
        "country": "BD",
        "timezone": "Asia/Dhaka",
        "assessment_system": "own",
        "default_language": "en",
        "retention_years": 7,
        # Sunday to Thursday: Friday and Saturday off.
        "school_days": ["7", "1", "2", "3", "4"],
    }
    assert client.post("/settings/school/", form).status_code == 302
    erp.school.refresh_from_db()
    assert erp.school.weekend_days == "5,6"
    form["school_days"] = []
    assert "Tick at least one school day" in client.post("/settings/school/", form).content.decode()


def test_the_school_week_page_saves_the_days(erp):
    client = login(erp.admin)
    client.post("/routine/week/", {"action": "days", "school_days": ["6", "7", "1", "2", "3"]})
    erp.school.refresh_from_db()
    assert erp.school.weekend_days == "4,5"
    assert login(erp.teacher).get("/routine/week/").status_code == 403


def test_the_routine_shows_only_the_days_the_school_meets(erp, day):
    body = login(erp.admin).get(f"/routine/?section={erp.section.pk}&academic_year={erp.year.pk}").content.decode()
    assert "Sunday" in body and "Thursday" in body and "Friday" not in body and "Saturday" not in body
    editor = login(erp.admin).get(f"/routine/edit/?section={erp.section.pk}&academic_year={erp.year.pk}")
    assert f'name="{FRIDAY}-{day[0].pk}-subject"' not in editor.content.decode()
    with pytest.raises(ValidationError, match="does not meet on Friday"):
        slot(erp, day[0], FRIDAY).full_clean(exclude=["school"])


# ------------------------------------------------------------------ building the day


def test_the_day_is_built_from_a_start_a_length_and_a_count(erp, day):
    rows = [(p.name, p.start_time, p.end_time, p.is_break) for p in Period.objects.filter(school=erp.school)]
    assert rows == [
        ("1st period", time(8), time(8, 45), False),
        ("2nd period", time(8, 45), time(9, 30), False),
        ("Tiffin", time(9, 30), time(10), True),
        ("3rd period", time(10), time(10, 45), False),
    ]
    assert AuditLog.objects.filter(action="routine.day_built").exists()


def test_a_day_in_use_is_not_rebuilt(erp, day):
    slot(erp, day[0], 1).save()
    with pytest.raises(ValidationError, match="already used in a routine"):
        build_day(school=erp.school, user=erp.admin, plan=plan_day(time(9), 40, 6))
    assert Period.objects.filter(school=erp.school).count() == 4


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((time(8), 45, 0), "between 1 and 15"),
        ((time(8), 5, 6), "between 10 and 180"),
        ((time(8), 45, 3, [(3, 30, "")]), "after one of periods 1 to 2"),
        ((time(22), 180, 3), "past midnight"),
    ],
)
def test_a_day_that_cannot_work_is_refused(args, message):
    with pytest.raises(ValidationError, match=message):
        plan_day(*args)


def test_the_page_builds_the_day(erp):
    client = login(erp.admin)
    client.post(
        "/routine/week/",
        {
            "action": "build",
            "first_start": "07:30",
            "minutes": "40",
            "count": "6",
            "break_after": "3",
            "break_minutes": "20",
            "break_name": "Tiffin",
        },
    )
    periods = list(Period.objects.filter(school=erp.school))
    assert len(periods) == 7 and periods[-1].end_time == time(11, 50)


# ------------------------------------------------------------------ a day with other timings


def test_a_shorter_thursday(erp, day):
    first, second, tiffin, third = day
    save_day_times(
        school=erp.school,
        user=erp.admin,
        weekday=THURSDAY,
        rows={
            first: (time(8), time(8, 35), False),
            second: (time(8, 35), time(9, 10), False),
            tiffin: (None, None, True),
            third: (None, None, True),
        },
    )
    assert PeriodDayTime.objects.filter(weekday=THURSDAY).count() == 4
    with pytest.raises(ValidationError, match="not held on Thursday"):
        slot(erp, third, THURSDAY).full_clean(exclude=["school"])
    body = login(erp.admin).get(f"/routine/?section={erp.section.pk}&academic_year={erp.year.pk}").content.decode()
    assert "08:35" in body and "Not held" in body
    # Back to the usual times: nothing separate is kept for the first period.
    save_day_times(
        school=erp.school,
        user=erp.admin,
        weekday=THURSDAY,
        rows={first: (time(8), time(8, 45), False)},
    )
    assert not PeriodDayTime.objects.filter(period=first).exists()


def test_overlapping_timings_on_a_day_are_refused(erp, day):
    first, second, _tiffin, _third = day
    with pytest.raises(ValidationError, match="overlap"):
        save_day_times(
            school=erp.school,
            user=erp.admin,
            weekday=THURSDAY,
            rows={first: (time(8), time(9), False), second: (time(8, 45), time(9, 30), False)},
        )


def test_clashes_follow_the_days_own_timings(erp, day):
    """On Thursday the 2nd period starts early and runs into the 1st: one teacher cannot do both."""
    first, second, _tiffin, _third = day
    other = Section.objects.create(school=erp.school, class_level=erp.level, name="C")
    slot(erp, first, THURSDAY, teacher=erp.employee).save()
    save_day_times(school=erp.school, user=erp.admin, weekday=THURSDAY, rows={first: (time(8), time(8, 45), False)})
    PeriodDayTime.objects.create(
        school=erp.school, period=second, weekday=THURSDAY, start_time=time(8, 30), end_time=time(9, 15)
    )
    with pytest.raises(ValidationError, match="already teaches"):
        slot(erp, second, THURSDAY, section=other, teacher=erp.employee).full_clean(exclude=["school"])
    assert erp.employee not in free_teachers(erp.school, erp.year, THURSDAY, second)
    # On other days the periods follow one another and the same teacher is free.
    slot(erp, second, 1, section=other, teacher=erp.employee).full_clean(exclude=["school"])


def test_lessons_left_on_days_no_longer_held_are_found_and_cleared(erp, day):
    lesson = slot(erp, day[0], SUNDAY)
    lesson.save()
    erp.school.weekend_days = "5,6,7"
    erp.school.save()
    assert stray_slots(erp.school) == [lesson]
    client = login(erp.admin)
    assert "no longer meets" in client.get("/routine/week/").content.decode()
    client.post("/routine/week/", {"action": "clear_stray"})
    assert not RoutineSlot.objects.exists()


def test_room_use_counts_only_periods_the_school_holds(erp, day):
    from timetable.models import Room

    Room.objects.create(school=erp.school, name="Room 1")
    # Five school days, three teaching periods; Thursday holds only two.
    PeriodDayTime.objects.create(school=erp.school, period=day[3], weekday=THURSDAY, not_held=True)
    [row] = room_utilisation(erp.school, erp.year)
    assert row["available"] == 14


def test_homework_falls_due_at_the_days_own_start(erp, day):
    from homework.services import _lesson_starts

    slot(erp, day[0], THURSDAY).save()
    PeriodDayTime.objects.create(
        school=erp.school, period=day[0], weekday=THURSDAY, start_time=time(7, 30), end_time=time(8, 15)
    )
    assert _lesson_starts(erp.section, erp.subject, erp.year)[THURSDAY] == time(7, 30)


def test_the_settings_card_leads_to_the_school_week(erp):
    body = login(erp.admin).get("/settings/").content.decode()
    assert "School week and periods" in body and "/routine/week/" in body
    assert date.today()  # the page itself renders for a manager
    assert login(erp.admin).get("/routine/week/?day=4").status_code == 200


def test_two_shifts_have_their_own_starts_periods_and_breaks(erp):
    client = login(erp.admin)
    assert client.get("/routine/period/new/?shift=morning").context["form"]["shift"].value() == "morning"
    assert "shift" in client.get(f"/settings/section/{erp.section.pk}/edit/").context["form"].fields
    morning = build_day(
        school=erp.school, user=erp.admin, shift="morning",
        plan=plan_day(time(8, 30), 40, 2, [(1, 20, "Morning break")]),
    )
    evening = build_day(
        school=erp.school, user=erp.admin, shift="evening",
        plan=plan_day(time(14), 35, 3),
    )
    erp.section.shift = "morning"
    erp.section.save(update_fields=["shift"])
    erp.other_section.shift = "evening"
    erp.other_section.save(update_fields=["shift"])

    morning_page = login(erp.admin).get(f"/routine/edit/?section={erp.section.pk}&academic_year={erp.year.pk}").content.decode()
    evening_page = login(erp.admin).get(
        f"/routine/edit/?section={erp.other_section.pk}&academic_year={erp.year.pk}"
    ).content.decode()
    assert "08:30" in morning_page and "Morning break" in morning_page
    assert f'name="1-{morning[0].pk}-subject"' in morning_page
    assert f'name="1-{evening[0].pk}-subject"' not in morning_page
    assert "14:00" in evening_page and "Morning break" not in evening_page
    assert f'name="1-{evening[0].pk}-subject"' in evening_page

    save_day_times(
        school=erp.school, user=erp.admin, weekday=THURSDAY,
        rows={morning[0]: (time(8), time(8, 40), False), morning[-1]: (None, None, True)},
    )
    morning_routine = login(erp.admin).get(f"/routine/?section={erp.section.pk}").content.decode()
    evening_routine = login(erp.admin).get(f"/routine/?section={erp.other_section.pk}").content.decode()
    assert "08:00" in morning_routine and "Not held" in morning_routine
    assert "14:00" in evening_routine and "08:00" not in evening_routine

    slot(erp, morning[0], 1, teacher=erp.employee).save()
    # The same teacher may take a later shift when the real times do not overlap.
    slot(erp, evening[0], 1, section=erp.other_section, teacher=erp.employee).full_clean(exclude=["school"])
    with pytest.raises(ValidationError, match="Morning shift"):
        slot(erp, evening[0], 1).full_clean(exclude=["school"])


def test_existing_default_routine_survives_adding_a_shift_schedule(erp, day):
    old_lesson = slot(erp, day[0], 1)
    old_lesson.save()
    erp.section.shift = "morning"
    erp.section.save(update_fields=["shift"])
    new_periods = build_day(
        school=erp.school, user=erp.admin, shift="morning", plan=plan_day(time(8, 30), 40, 2)
    )
    routine = login(erp.admin).get(f"/routine/?section={erp.section.pk}&academic_year={erp.year.pk}").content.decode()
    assert old_lesson.subject.name in routine
    editor = login(erp.admin).get(f"/routine/edit/?section={erp.section.pk}&academic_year={erp.year.pk}").content.decode()
    assert f'name="1-{new_periods[0].pk}-subject"' in editor
    assert f'name="1-{day[0].pk}-subject"' in editor


def test_room_capacity_counts_both_shifts_without_unused_starter_periods(erp, day):
    from timetable.models import Room

    Room.objects.create(school=erp.school, name="One classroom")
    erp.section.shift = "morning"
    erp.section.save(update_fields=["shift"])
    erp.other_section.shift = "evening"
    erp.other_section.save(update_fields=["shift"])
    build_day(school=erp.school, user=erp.admin, shift="morning", plan=plan_day(time(8), 40, 2))
    build_day(school=erp.school, user=erp.admin, shift="evening", plan=plan_day(time(14), 40, 3))
    [row] = room_utilisation(erp.school, erp.year)
    assert row["available"] == 25  # Five days times two morning and three evening lessons.


def test_changing_a_weekday_time_cannot_double_book_a_teacher_across_shifts(erp):
    morning = build_day(
        school=erp.school, user=erp.admin, shift="morning", plan=plan_day(time(8), 40, 1)
    )[0]
    evening = build_day(
        school=erp.school, user=erp.admin, shift="evening", plan=plan_day(time(14), 40, 1)
    )[0]
    erp.section.shift = "morning"
    erp.section.save(update_fields=["shift"])
    erp.other_section.shift = "evening"
    erp.other_section.save(update_fields=["shift"])
    slot(erp, morning, 1, teacher=erp.employee).save()
    slot(erp, evening, 1, section=erp.other_section, teacher=erp.employee).save()
    with pytest.raises(ValidationError, match="Move the lessons first"):
        save_day_times(
            school=erp.school, user=erp.admin, weekday=1,
            rows={morning: (time(14, 10), time(14, 50), False)},
        )
    assert not PeriodDayTime.objects.filter(period=morning, weekday=1).exists()
