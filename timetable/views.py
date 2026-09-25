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

from .models import WEEKDAYS, Period, Room, RoutineSlot
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
            for weekday, _label in WEEKDAYS:
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

    current = {}
    if section and year:
        for slot in RoutineSlot.objects.filter(school=request.school, academic_year=year, section=section):
            current[(slot.weekday, slot.period_id)] = slot

    rows = []
    for period in periods:
        cells = []
        for weekday, label in WEEKDAYS:
            slot = current.get((weekday, period.pk))
            cells.append(
                {
                    "weekday": weekday,
                    "label": label,
                    "prefix": f"{weekday}-{period.pk}",
                    "slot": slot,
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
            "weekdays": WEEKDAYS,
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
