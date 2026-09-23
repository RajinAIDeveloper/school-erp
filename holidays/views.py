"""The school calendar: list, month grid, national-holiday import and iCalendar export."""

from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from academics.models import AcademicYear
from core.access import require_permission

from .models import Holiday
from .services import import_national_holidays, month_calendar, year_summary


def ical_escape(text):
    """
    Escape a value for iCalendar (RFC 5545 section 3.3.11).

    A newline becomes the two characters backslash-n. Writing an actual newline into a
    property value ends the line as far as a parser is concerned, and a multi-line holiday
    description then produces a file that Google Calendar and Outlook simply refuse.
    """
    return (
        str(text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\r", "\\n")
        .replace("\n", "\\n")
    )


def ical_line(name, value):
    """
    One content line, folded to 75 octets as the specification requires.

    Folding counts bytes, not characters: a Bangla holiday name is three bytes per letter,
    so a name that looks short can still overrun the limit and be rejected.
    """
    raw = f"{name}:{value}".encode()
    if len(raw) <= 75:
        return [raw.decode()]
    lines, rest = [], raw
    limit = 75
    while len(rest) > limit:
        cut = limit
        # Never split a multi-byte character across two folded lines.
        while cut > 0 and (rest[cut] & 0xC0) == 0x80:
            cut -= 1
        lines.append(rest[:cut].decode())
        rest = rest[cut:]
        limit = 74  # continuation lines carry a leading space
    lines.append(rest.decode())
    return [lines[0]] + [" " + line for line in lines[1:]]


@require_permission("holidays.view_holiday")
def calendar_export(request):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//School ERP//School Calendar//EN", "CALSCALE:GREGORIAN"]
    for h in Holiday.objects.filter(school=request.school):
        lines += [
            "BEGIN:VEVENT",
            f"UID:holiday-{h.pk}@school-erp",
            f"DTSTAMP:{h.updated_at:%Y%m%dT%H%M%SZ}",
            f"DTSTART;VALUE=DATE:{h.start_date:%Y%m%d}",
            f"DTEND;VALUE=DATE:{h.end_date + timedelta(days=1):%Y%m%d}",
            *ical_line("SUMMARY", ical_escape(h.name)),
            *ical_line("DESCRIPTION", ical_escape(h.description)),
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    response = HttpResponse("\r\n".join(lines) + "\r\n", content_type="text/calendar")
    response["Content-Disposition"] = 'attachment; filename="school-calendar.ics"'
    return response


def _month(raw):
    """A YYYY-MM parameter, falling back to the current month."""
    today = timezone.localdate()
    try:
        year, month = (int(part) for part in str(raw).split("-")[:2])
        if 1900 <= year <= 2999 and 1 <= month <= 12:
            return year, month
    except (TypeError, ValueError):
        pass
    return today.year, today.month


@require_permission("holidays.view_holiday")
def month_view(request):
    """The calendar everyone actually reads: a month, with closures marked."""
    year, month = _month(request.GET.get("month"))
    grid = month_calendar(request.school, year, month)
    academic_year = AcademicYear.current_for(request.school)
    upcoming = Holiday.objects.filter(school=request.school, end_date__gte=timezone.localdate()).order_by("start_date")[
        :8
    ]
    return render(
        request,
        "holidays/calendar.html",
        {
            "grid": grid,
            "upcoming": upcoming,
            "summary": year_summary(request.school, academic_year),
            "academic_year": academic_year,
            "page_title": f"School calendar · {grid['label']}",
        },
    )


@require_permission("holidays.add_holiday")
@require_POST
def import_national(request):
    year = request.POST.get("year", "")
    year = int(year) if year.isdigit() else timezone.localdate().year
    created = import_national_holidays(request.school, year, request.user)
    if created:
        messages.success(
            request, f"Added {len(created)} national holiday(s) for {year}. Religious dates still need entering."
        )
    else:
        messages.info(request, f"The fixed-date national holidays for {year} are already on the calendar.")
    return redirect("holidays:list")
