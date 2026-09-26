"""Class routine: week grids, a bulk editor and utilisation reports."""

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from academics.models import AcademicYear, Section, Subject
from core.access import is_manager, plans_timetable, require_permission, sections_for, students_for
from core.exports import spreadsheet
from core.forms import TailwindFormMixin
from core.pdf import table_document
from employees.models import Employee

from .models import WEEKDAYS, Period, Room, RoutineSlot, day_times, school_weekdays, times_on
from .services import (
    WeekGridError,
    all_periods,
    free_teachers,
    room_utilisation,
    save_week,
    teacher_load,
    week_grid,
)


class RoutineFilter(TailwindFormMixin, forms.Form):
    academic_year = forms.ModelChoiceField(queryset=AcademicYear.objects.none(), required=False)
    section = forms.ModelChoiceField(queryset=Section.objects.none(), required=False)
    teacher = forms.ModelChoiceField(queryset=Employee.objects.none(), required=False)

    def __init__(self, *args, school, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(school=school)
        self.fields["section"].queryset = Section.objects.filter(school=school).select_related("class_level")
        self.fields["teacher"].queryset = Employee.objects.filter(school=school, employee_type=Employee.Type.TEACHER)
        if not is_manager(user):
            self.fields["teacher"].queryset = self.fields["teacher"].queryset.filter(user=user)


def _pick(queryset, raw, required=True):
    """
    Resolve a select's value.

    Blank means "none chosen". A value that is present but matches nothing is a problem,
    not a blank: returning None there would silently clear a period that the person was
    trying to set. Optional fields answer False so the caller can tell the two apart.
    """
    if not str(raw).strip():
        return None
    chosen = queryset.filter(pk=int(raw)).first() if str(raw).isdigit() else None
    if chosen is None and not required:
        return False
    return chosen


def visible_slots(request, queryset):
    """Managers see the whole school; everyone else sees a routine that concerns them."""
    if is_manager(request.user):
        return queryset
    return queryset.filter(
        Q(section__in=sections_for(request.user, request.school))
        | Q(section__enrollments__student__in=students_for(request.user, request.school))
        | Q(teacher__user=request.user)
    ).distinct()


@require_permission("timetable.view_routineslot", also="own class or children unless a manager")
def routine(request):
    """The week as a grid, for a class or for a teacher."""
    form = RoutineFilter(request.GET or None, school=request.school, user=request.user)
    year = section = teacher = None
    if form.is_bound and form.is_valid():
        year = form.cleaned_data["academic_year"]
        section = form.cleaned_data["section"]
        teacher = form.cleaned_data["teacher"]
    year = year or AcademicYear.current_for(request.school)

    # A student or guardian with no filter chosen still gets their own class.
    if section is None and teacher is None and not is_manager(request.user):
        own = sections_for(request.user, request.school).first()
        if own is None:
            enrolled = students_for(request.user, request.school).values_list("enrollments__section", flat=True)
            own = Section.objects.filter(school=request.school, pk__in=[s for s in enrolled if s]).first()
        section = own

    grid = week_grid(request.school, year, section=section, teacher=teacher) if year else None
    if grid and not is_manager(request.user):
        allowed = set(
            visible_slots(request, RoutineSlot.objects.filter(school=request.school)).values_list("pk", flat=True)
        )
        for row in grid["rows"]:
            for cell in row["cells"]:
                cell["slots"] = [slot for slot in cell["slots"] if slot.pk in allowed]

    slots = (
        visible_slots(
            request,
            RoutineSlot.objects.filter(school=request.school, academic_year=year).select_related(
                "academic_year", "section__class_level", "teacher", "subject", "room", "period"
            ),
        )
        if year
        else RoutineSlot.objects.none()
    )
    if section:
        slots = slots.filter(section=section)
    if teacher:
        slots = slots.filter(teacher=teacher)

    fmt = request.GET.get("format")
    if fmt:
        headers = ["Day", "Period", "Class / section", "Subject", "Teacher", "Room"]
        rows = [
            [
                s.get_weekday_display(),
                str(s.period),
                str(s.section),
                str(s.subject),
                str(s.teacher or ""),
                str(s.room or ""),
            ]
            for s in slots.order_by("weekday", "period__order")
        ]
        title = "Class routine"
        if section:
            title = f"Routine - {section}"
        elif teacher:
            title = f"Routine - {teacher.full_name}"
        if fmt == "pdf":
            return table_document(
                request.school, title, headers, rows, subtitle=str(year or ""), filename="routine.pdf"
            )
        if fmt in ("csv", "xlsx"):
            return spreadsheet("routine", headers, rows, fmt)

    return render(
        request,
        "timetable/routine.html",
        {
            "form": form,
            "grid": grid,
            "year": year,
            "section": section,
            "teacher": teacher,
            "can_edit": request.user.has_perm("timetable.change_routineslot"),
            "page_title": "Class routine",
        },
    )


@require_permission("timetable.change_routineslot")
def grid_edit(request):
    """Set a whole week for one section on one screen."""
    year_pk = request.POST.get("academic_year") or request.GET.get("academic_year")
    section_pk = request.POST.get("section") or request.GET.get("section")
    year = (
        get_object_or_404(AcademicYear, school=request.school, pk=year_pk)
        if str(year_pk).isdigit()
        else AcademicYear.current_for(request.school)
    )
    section = get_object_or_404(Section, school=request.school, pk=section_pk) if str(section_pk).isdigit() else None
    periods = all_periods(request.school)
    weekdays = school_weekdays(request.school)
    weekend = request.school.weekend_day_numbers
    timings = day_times(request.school)
    # Retired master data stays on the historical rows that use it, but is not offered
    # for a new one: a school that closed a room should not be able to book it again.
    subjects = Subject.objects.filter(school=request.school, is_active=True).order_by("name")
    teachers = Employee.objects.filter(
        school=request.school, employee_type=Employee.Type.TEACHER, status=Employee.Status.ACTIVE
    ).order_by("first_name")
    rooms = Room.objects.filter(school=request.school, is_active=True).order_by("name")
    cell_errors = {}

    if request.method == "POST" and section and year:
        cells = {}
        cell_errors = {}
        for period in periods:
            if period.is_break or not period.is_active:
                continue
            for weekday, _label in weekdays:
                if times_on(period, weekday, timings) is None:
                    continue
                prefix = f"{weekday}-{period.pk}"
                subject_pk = request.POST.get(f"{prefix}-subject", "")
                if not subject_pk:
                    cells[(weekday, period.pk)] = None
                    continue
                # A non-blank value that resolves to nothing is a tampered or stale form,
                # not an instruction to clear the period. The whole grid is refused.
                subject = _pick(subjects, subject_pk)
                if subject is None:
                    cell_errors[(weekday, period.pk)] = "That subject is not available. Reload the page."
                    continue
                teacher = _pick(teachers, request.POST.get(f"{prefix}-teacher", ""), required=False)
                room = _pick(rooms, request.POST.get(f"{prefix}-room", ""), required=False)
                if teacher is False:
                    cell_errors[(weekday, period.pk)] = "That teacher is not available. Reload the page."
                    continue
                if room is False:
                    cell_errors[(weekday, period.pk)] = "That room is not available. Reload the page."
                    continue
                cells[(weekday, period.pk)] = {"subject": subject, "teacher": teacher, "room": room}
        # A lesson on a day the school no longer meets, or in a period not held that day, goes
        # with the week it belongs to. A retired period keeps what it holds.
        for slot in RoutineSlot.objects.filter(school=request.school, academic_year=year, section=section):
            key = (slot.weekday, slot.period_id)
            if key in cells or key in cell_errors or slot.period.is_break or not slot.period.is_active:
                continue
            if slot.weekday in weekend or times_on(slot.period, slot.weekday, timings) is None:
                cells[key] = None
        if cell_errors:
            messages.error(request, "Nothing was saved. The cells marked below could not be read.")
            cells = None
        try:
            if cells is not None:
                saved, cleared = save_week(
                    school=request.school, user=request.user, academic_year=year, section=section, cells=cells
                )
                messages.success(request, f"Saved {saved} period(s); cleared {cleared}.")
                return_to = request.GET.get("return_to", "")
                if return_to.startswith("/") and not return_to.startswith("//") and url_has_allowed_host_and_scheme(
                    return_to, allowed_hosts={request.get_host()}, require_https=request.is_secure()
                ):
                    return redirect(return_to)
                return redirect(f"/routine/?academic_year={year.pk}&section={section.pk}")
        except WeekGridError as exc:
            cell_errors = exc.errors
            messages.error(request, "Nothing was saved. The cells marked below clash with another class.")

    current, stray = {}, 0
    if section and year:
        for slot in RoutineSlot.objects.filter(
            school=request.school, academic_year=year, section=section
        ).select_related("period"):
            current[(slot.weekday, slot.period_id)] = slot
            if slot.period.is_active and (
                slot.weekday in weekend or times_on(slot.period, slot.weekday, timings) is None
            ):
                stray += 1

    rows = []
    for period in periods:
        cells = []
        for weekday, label in weekdays:
            slot = current.get((weekday, period.pk))
            times = times_on(period, weekday, timings)
            cells.append(
                {
                    "weekday": weekday,
                    "label": label,
                    "prefix": f"{weekday}-{period.pk}",
                    "slot": slot,
                    "off": times is None,
                    "times": times if times and times != (period.start_time, period.end_time) else None,
                    "error": cell_errors.get((weekday, period.pk)),
                }
            )
        # A retired period still shows what it used to hold, but takes nothing new.
        rows.append({"period": period, "cells": cells, "retired": not period.is_active})

    return render(
        request,
        "timetable/grid_edit.html",
        {
            "year": year,
            "section": section,
            # Retired sections keep their history on the routine screen, but a new week
            # is never built into one.
            "sections": Section.objects.filter(school=request.school, is_active=True).select_related("class_level"),
            "years": AcademicYear.objects.filter(school=request.school),
            "weekdays": weekdays,
            "stray": stray,
            "rows": rows,
            "subjects": subjects,
            "teachers": teachers,
            "rooms": rooms,
            "page_title": f"Edit routine{f' - {section}' if section else ''}",
        },
    )


@require_permission("timetable.view_routineslot", also="timetable editors only")
def free_teachers_json(request):
    """Who is free in a given slot, for the editor's helper."""
    if not plans_timetable(request.user):
        raise PermissionDenied("This helper is for staff who plan the timetable.")
    raw_year = request.GET.get("academic_year", "")
    year = (
        AcademicYear.objects.filter(school=request.school, pk=raw_year).first()
        if str(raw_year).isdigit()
        else AcademicYear.current_for(request.school)
    )
    weekday = request.GET.get("weekday", "")
    period = Period.objects.filter(school=request.school, pk=request.GET.get("period", 0)).first()
    if not (year and period and weekday.isdigit()):
        return JsonResponse([], safe=False)
    rows = free_teachers(request.school, year, int(weekday), period)
    return JsonResponse([{"id": t.pk, "name": t.full_name} for t in rows], safe=False)


@require_permission("timetable.view_routineslot", also="timetable editors only")
def utilisation(request):
    """Room use and teacher load for the current year."""
    if not plans_timetable(request.user):
        raise PermissionDenied("This report is for staff who plan the timetable.")
    year = AcademicYear.current_for(request.school)
    rooms = room_utilisation(request.school, year) if year else []
    load = teacher_load(request.school, year) if year else []
    fmt = request.GET.get("format")
    if fmt in ("csv", "xlsx", "pdf"):
        headers = ["Room", "Capacity", "Periods used", "Periods free", "Use %"]
        rows = [[r["room"].name, r["capacity"], r["used"], r["available"], r["percent"]] for r in rooms]
        if fmt == "pdf":
            return table_document(
                request.school,
                "Room utilisation",
                headers,
                rows,
                subtitle=str(year or ""),
                filename="room-utilisation.pdf",
                align_right=(1, 2, 3, 4),
            )
        return spreadsheet("room-utilisation", headers, rows, fmt)
    return render(
        request,
        "timetable/utilisation.html",
        {"rooms": rooms, "load": load, "year": year, "page_title": "Routine utilisation"},
    )


def _clock(raw):
    from datetime import time

    raw = (raw or "").strip()
    try:
        hour, minute = raw.split(":")[:2]
        return time(int(hour), int(minute))
    except (TypeError, ValueError):
        return None


def _number(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


@require_permission("timetable.change_period", also="the school's managers")
def school_week(request):
    """
    The school week on one page: which days the school meets, the periods of the day and
    their times, days that keep other timings, and lessons left on days no longer held.
    """
    from django.core.exceptions import ValidationError

    from core.forms import school_days_field, school_days_of, weekend_for

    from .services import build_day, clear_stray_slots, plan_day, save_day_times, stray_slots

    school = request.school
    weekdays = dict(WEEKDAYS)
    chosen_day = _number(request.GET.get("day") or request.POST.get("day"))
    if chosen_day not in weekdays:
        chosen_day = next((n for n, _label in school_weekdays(school)), 4)
    periods = list(Period.objects.filter(school=school, is_active=True).order_by("order"))

    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            if action == "days":
                field = school_days_field()
                days = field.clean(request.POST.getlist("school_days"))
                school.weekend_days = weekend_for(days)
                school.save(update_fields=["weekend_days", "updated_at"])
                messages.success(request, "School days saved.")
            elif action == "build":
                first_start = _clock(request.POST.get("first_start"))
                minutes, count = _number(request.POST.get("minutes")), _number(request.POST.get("count"))
                if first_start is None or minutes is None or count is None:
                    raise ValidationError("Give when the first period starts, how long a period lasts and how many.")
                breaks = []
                for suffix in ("", "_2"):
                    after = _number(request.POST.get(f"break_after{suffix}"))
                    if after:
                        breaks.append(
                            (
                                after,
                                _number(request.POST.get(f"break_minutes{suffix}")) or 0,
                                request.POST.get(f"break_name{suffix}", ""),
                            )
                        )
                made = build_day(school=school, user=request.user, plan=plan_day(first_start, minutes, count, breaks))
                messages.success(
                    request, f"{len(made)} periods set, from {made[0].start_time:%H:%M} to {made[-1].end_time:%H:%M}."
                )
            elif action == "day_times":
                rows = {
                    period: (
                        _clock(request.POST.get(f"start_{period.pk}")),
                        _clock(request.POST.get(f"end_{period.pk}")),
                        request.POST.get(f"off_{period.pk}") == "on",
                    )
                    for period in periods
                }
                changed = save_day_times(school=school, user=request.user, weekday=chosen_day, rows=rows)
                messages.success(request, f"{weekdays[chosen_day]} saved: {changed} period(s) changed.")
            elif action == "clear_stray":
                cleared = clear_stray_slots(school=school, user=request.user)
                messages.success(request, f"{cleared} lesson(s) removed from the routine.")
            else:
                raise ValidationError("Choose what to save.")
        except ValidationError as problem:
            for message in problem.messages:
                messages.error(request, message)
        except PermissionDenied:
            messages.error(request, "Your role cannot change the school week.")
        return redirect(f"{request.path}?day={chosen_day}")

    timings = day_times(school)
    day_rows = []
    for period in periods:
        times = times_on(period, chosen_day, timings)
        day_rows.append(
            {
                "period": period,
                "start": times[0] if times else period.start_time,
                "end": times[1] if times else period.end_time,
                "off": times is None,
                "changed": (period.pk, chosen_day) in timings,
            }
        )
    special = {}
    for row in timings.values():
        special.setdefault(row.weekday, 0)
        special[row.weekday] += 1
    days_field = school_days_field()
    return render(
        request,
        "timetable/week.html",
        {
            "weekdays": WEEKDAYS,
            "school_days": school_days_of(school),
            "days_help": days_field.help_text,
            "periods": periods,
            "used": RoutineSlot.objects.filter(school=school).exists(),
            "chosen_day": chosen_day,
            "chosen_label": weekdays[chosen_day],
            "day_rows": day_rows,
            "special": [(weekdays[day], count, day) for day, count in sorted(special.items())],
            "stray": len(stray_slots(school)),
            "page_title": "School week and periods",
        },
    )
