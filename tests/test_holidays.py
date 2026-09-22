"""The month calendar, national-holiday import and working-day counting."""

from datetime import date

from django.test import Client

from core.models import AuditLog
from holidays.models import Holiday, is_holiday
from holidays.services import import_national_holidays, month_calendar, working_days, year_summary


def cells(grid):
    return {cell["date"]: cell for week in grid["weeks"] for cell in week}


def test_month_grid_marks_weekends_holidays_and_the_current_month(erp):
    Holiday.objects.create(
        school=erp.school, name="Autumn break", start_date=date(2026, 9, 10), end_date=date(2026, 9, 12)
    )
    grid = month_calendar(erp.school, 2026, 9)
    by_date = cells(grid)

    assert grid["label"] == "September 2026"
    assert grid["weekday_labels"][0] == "Sat"
    # 11 September 2026 is a Friday, a weekend day for this school and inside the break.
    friday = by_date[date(2026, 9, 11)]
    assert friday["is_weekend"] and friday["is_closed"]
    assert [h.name for h in by_date[date(2026, 9, 10)]["holidays"]] == ["Autumn break"]
    assert by_date[date(2026, 9, 14)]["holidays"] == []
    assert by_date[date(2026, 9, 14)]["in_month"]
    assert not by_date[date(2026, 9, 14)]["is_closed"]


def test_month_grid_pads_with_neighbouring_days_marked_out_of_month(erp):
    grid = month_calendar(erp.school, 2026, 9)
    padding = [cell for week in grid["weeks"] for cell in week if not cell["in_month"]]
    assert padding
    assert all(cell["date"].month != 9 for cell in padding)
    assert all(len(week) == 7 for week in grid["weeks"])


def test_month_grid_counts_open_days(erp):
    before = month_calendar(erp.school, 2026, 9)["open_days"]
    Holiday.objects.create(school=erp.school, name="Break", start_date=date(2026, 9, 14), end_date=date(2026, 9, 16))
    after = month_calendar(erp.school, 2026, 9)["open_days"]
    assert after == before - 3


def test_an_open_event_does_not_close_the_school(erp):
    Holiday.objects.create(
        school=erp.school,
        name="Sports day",
        holiday_type="event",
        closes_school=False,
        start_date=date(2026, 9, 14),
        end_date=date(2026, 9, 14),
    )
    grid = month_calendar(erp.school, 2026, 9)
    cell = cells(grid)[date(2026, 9, 14)]
    assert cell["holidays"] and not cell["is_closed"]
    assert not is_holiday(erp.school, date(2026, 9, 14))


def test_working_days_exclude_weekends_and_closures(erp):
    # 14 to 20 September 2026: Monday to Sunday, with Friday and Saturday the weekend.
    assert working_days(erp.school, date(2026, 9, 14), date(2026, 9, 20)) == 5
    Holiday.objects.create(
        school=erp.school, name="Civic day", start_date=date(2026, 9, 16), end_date=date(2026, 9, 16)
    )
    assert working_days(erp.school, date(2026, 9, 14), date(2026, 9, 20)) == 4
    assert working_days(erp.school, date(2026, 9, 20), date(2026, 9, 14)) == 0


def test_year_summary_covers_every_month_of_the_session(erp):
    rows = year_summary(erp.school, erp.year)
    assert len(rows) == 12
    assert rows[0]["label"] == "January 2026"
    assert all(row["working"] <= row["total"] for row in rows)


def test_national_import_adds_fixed_dates_once(erp):
    created = import_national_holidays(erp.school, 2026, erp.admin)
    names = {holiday.name for holiday in created}
    assert "Independence Day" in names
    assert "Victory Day" in names
    assert Holiday.objects.get(school=erp.school, name="Victory Day").start_date == date(2026, 12, 16)
    assert AuditLog.objects.filter(action="holidays.imported").exists()

    assert import_national_holidays(erp.school, 2026, erp.admin) == []
    assert Holiday.objects.filter(school=erp.school, name="Victory Day").count() == 1


def test_national_import_is_per_school_and_per_year(erp):
    import_national_holidays(erp.school, 2026, erp.admin)
    assert not Holiday.objects.filter(school=erp.other).exists()
    created = import_national_holidays(erp.school, 2027, erp.admin)
    assert created and Holiday.objects.filter(school=erp.school, name="Victory Day").count() == 2


def test_calendar_screen_renders_and_imports(erp):
    Holiday.objects.create(
        school=erp.school, name="Autumn break", start_date=date(2026, 9, 10), end_date=date(2026, 9, 12)
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/holidays/calendar/?month=2026-09").content
    assert b"September 2026" in body
    assert b"Autumn break" in body
    assert b"Working days" in body

    assert client.post("/holidays/import-national/", {"year": "2026"}).status_code == 302
    assert Holiday.objects.filter(school=erp.school, name="May Day").exists()


def test_calendar_handles_a_nonsense_month(erp):
    client = Client()
    client.force_login(erp.admin)
    assert client.get("/holidays/calendar/?month=banana").status_code == 200


def test_calendar_is_visible_to_families_but_import_is_not(erp):
    client = Client()
    client.force_login(erp.parent)
    assert client.get("/holidays/calendar/").status_code == 200
    assert client.post("/holidays/import-national/", {"year": "2026"}).status_code == 403


def test_ical_export_still_carries_the_right_dates(erp):
    Holiday.objects.create(
        school=erp.school, name="Autumn break", start_date=date(2026, 10, 1), end_date=date(2026, 10, 2)
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/holidays/calendar.ics").content
    assert b"DTSTART;VALUE=DATE:20261001" in body
    # iCalendar end dates are exclusive, so a two-day holiday ends on the third.
    assert b"DTEND;VALUE=DATE:20261003" in body
