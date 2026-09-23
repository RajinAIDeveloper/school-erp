"""
The school calendar.

A holiday is not only an entry in a list: it decides whether a register may be taken,
how many working days a month has, and what a parent sees when they plan. This module
turns the stored ranges into the month grid and the counts everything else asks for.
"""

import calendar
from datetime import date, timedelta

from django.db import transaction

from core.models import AuditLog

from .models import Holiday

# Fixed-date national holidays observed by Bangladeshi schools, with the years each one
# actually applies to. Religious dates move with the lunar calendar and are deliberately
# left out: a school enters those from the government's annual notice rather than trusting
# a guess baked into software.
#
# The list is versioned by year because a public holiday is a political decision, not a
# fact about the calendar. Importing a year should reproduce what that year's notice said,
# so a school looking back at 2023 sees the calendar it actually kept.
#
# Sources: the Ministry of Public Administration's public holiday page,
# https://mopa.gov.bd/pages/public-holiday, and its annual holiday notice for the year in
# question.
#
# name, month, day, type, first year observed, last year observed (None = still observed)
BD_FIXED_HOLIDAYS = [
    ("International Mother Language Day", 2, 21, Holiday.Type.PUBLIC, None, None),
    ("Independence Day", 3, 26, Holiday.Type.PUBLIC, None, None),
    ("Bengali New Year (Pohela Boishakh)", 4, 14, Holiday.Type.PUBLIC, None, None),
    ("May Day", 5, 1, Holiday.Type.PUBLIC, None, None),
    # Cancelled as a public holiday by the MOPA notice of 13 August 2024,
    # "১৫ আগস্ট এর সাধারণ ছুটি বাতিল". It was a holiday up to and including 2023, so a school
    # importing an earlier year still gets the calendar that year was kept to.
    ("National Mourning Day", 8, 15, Holiday.Type.PUBLIC, None, 2023),
    ("Victory Day", 12, 16, Holiday.Type.PUBLIC, None, None),
    ("Christmas Day", 12, 25, Holiday.Type.RELIGIOUS, None, None),
]


def fixed_holidays_for(year):
    """The fixed-date holidays that applied in one year."""
    return [
        (name, month, day, kind)
        for name, month, day, kind, first, last in BD_FIXED_HOLIDAYS
        if (first is None or year >= first) and (last is None or year <= last)
    ]


@transaction.atomic
def import_national_holidays(school, year, user=None):
    """
    Add the fixed-date national holidays for a year, skipping any already entered.

    Only the dates that were holidays in that year are added; see BD_FIXED_HOLIDAYS for
    which notice each one rests on. Returns the ones created, so the screen can say what
    it actually did.
    """
    created = []
    for name, month, day, kind in fixed_holidays_for(year):
        when = date(year, month, day)
        if Holiday.objects.filter(school=school, name=name, start_date=when).exists():
            continue
        created.append(
            Holiday.objects.create(
                school=school,
                name=name,
                holiday_type=kind,
                start_date=when,
                end_date=when,
                description="Imported from the fixed-date national holiday list.",
            )
        )
    if user is not None:
        AuditLog.objects.create(
            school=school,
            user=user,
            action="holidays.imported",
            description=f"{len(created)} national holiday(s) added for {year}",
        )
    return created


def month_calendar(school, year, month):
    """
    The month as weeks of day cells, each marked closed, weekend or open.

    Weeks start on Saturday, which is how a Bangladeshi school week reads.
    """
    from .models import holiday_dates_between

    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    closed = holiday_dates_between(school, first, last)
    weekend = school.weekend_day_numbers

    events = {}
    for holiday in Holiday.objects.filter(school=school, start_date__lte=last, end_date__gte=first):
        for day in holiday.dates():
            if first <= day <= last:
                events.setdefault(day, []).append(holiday)

    # Saturday is weekday 6 in ISO terms; shift so the grid starts there.
    lead = (first.isoweekday() - 6) % 7
    cursor = first - timedelta(days=lead)
    weeks, week = [], []
    today = date.today()
    while cursor <= last or len(week) % 7:
        in_month = cursor.month == month and cursor.year == year
        week.append(
            {
                "date": cursor,
                "in_month": in_month,
                "is_today": cursor == today,
                "is_weekend": cursor.isoweekday() in weekend,
                "is_closed": cursor in closed,
                "holidays": events.get(cursor, []),
            }
        )
        if len(week) == 7:
            weeks.append(week)
            week = []
        cursor += timedelta(days=1)
    if week:
        weeks.append(week)

    open_days = [
        cell["date"]
        for row in weeks
        for cell in row
        if cell["in_month"] and not cell["is_closed"] and not cell["is_weekend"]
    ]
    return {
        "year": year,
        "month": month,
        "label": f"{calendar.month_name[month]} {year}",
        "weeks": weeks,
        "weekday_labels": ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"],
        "open_days": len(open_days),
        "closed_days": sum(
            1 for row in weeks for cell in row if cell["in_month"] and (cell["is_closed"] or cell["is_weekend"])
        ),
        "previous": (first - timedelta(days=1)).strftime("%Y-%m"),
        "next": (last + timedelta(days=1)).strftime("%Y-%m"),
    }


def working_days(school, start, end):
    """Days the school is actually open in a range, weekends and holidays removed."""
    from .models import holiday_dates_between

    if not start or not end or end < start:
        return 0
    closed = holiday_dates_between(school, start, end)
    weekend = school.weekend_day_numbers
    count, cursor = 0, start
    while cursor <= end:
        if cursor not in closed and cursor.isoweekday() not in weekend:
            count += 1
        cursor += timedelta(days=1)
    return count


def year_summary(school, academic_year):
    """Working days per month across an academic year, for planning."""
    if academic_year is None:
        return []
    rows = []
    cursor = academic_year.start_date.replace(day=1)
    while cursor <= academic_year.end_date:
        last = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        window_start = max(cursor, academic_year.start_date)
        window_end = min(last, academic_year.end_date)
        rows.append(
            {
                "month": cursor,
                "label": f"{calendar.month_name[cursor.month]} {cursor.year}",
                "working": working_days(school, window_start, window_end),
                "total": (window_end - window_start).days + 1,
            }
        )
        cursor = (last + timedelta(days=1)).replace(day=1)
    return rows
