"""
Timetable assembly.

A routine is read as a grid — days across, periods down — so that is how it is built
here. The editor saves a whole week at once and validates every cell first, because a
half-saved timetable is worse than none: it silently double-books a teacher.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from core.access import assert_actor_school, assert_school
from core.models import AuditLog

from .models import WEEKDAYS, Period, RoutineSlot


def teaching_periods(school):
    return list(Period.objects.filter(school=school, is_break=False).order_by("order"))


def all_periods(school):
    return list(Period.objects.filter(school=school).order_by("order"))


def week_grid(school, academic_year, *, section=None, teacher=None):
    """
    Build the week as rows of periods and columns of days.

    Passing a section gives a class routine; passing a teacher gives that teacher's own
    week, which is what a staff room actually pins to the wall.
    """
    periods = all_periods(school)
    slots = RoutineSlot.objects.filter(school=school, academic_year=academic_year).select_related(
        "subject", "teacher", "room", "period", "section__class_level"
    )
    if section is not None:
        slots = slots.filter(section=section)
    if teacher is not None:
        slots = slots.filter(teacher=teacher)

    by_cell = {}
    for slot in slots:
        by_cell.setdefault((slot.period_id, slot.weekday), []).append(slot)

    rows = []
    for period in periods:
        cells = []
        for weekday, label in WEEKDAYS:
            cells.append(
                {
                    "weekday": weekday,
                    "label": label,
                    "slots": sorted(by_cell.get((period.pk, weekday), []), key=lambda s: str(s.section)),
                }
            )
        rows.append({"period": period, "cells": cells})
    return {"periods": periods, "weekdays": WEEKDAYS, "rows": rows}


def free_teachers(school, academic_year, weekday, period, employee_type="teacher"):
    """Who is not already teaching in this slot."""
    from employees.models import Employee

    busy = RoutineSlot.objects.filter(
        school=school, academic_year=academic_year, weekday=weekday, period=period, teacher__isnull=False
    ).values_list("teacher_id", flat=True)
    return (
        Employee.objects.filter(school=school, employee_type=employee_type, status=Employee.Status.ACTIVE)
        .exclude(pk__in=busy)
        .order_by("first_name", "last_name")
    )


@transaction.atomic
def save_week(*, school, user, academic_year, section, cells):
    """
    Replace one section's week in a single transaction.

    `cells` maps (weekday, period_id) to {"subject", "teacher", "room"} or None to clear.
    Every cell is validated against the rest of the school's timetable before anything is
    written, and the errors come back keyed by cell so the grid can show them in place.
    """
    assert_actor_school(user, school)
    assert_school(school, academic_year, section)
    if not user.has_perm("timetable.change_routineslot"):
        raise PermissionDenied

    existing = {
        (slot.weekday, slot.period_id): slot
        for slot in RoutineSlot.objects.filter(
            school=school, academic_year=academic_year, section=section
        ).select_for_update()
    }

    errors, planned, removals = {}, [], []
    for (weekday, period_id), values in cells.items():
        current = existing.get((weekday, period_id))
        if values is None or not values.get("subject"):
            if current:
                removals.append(current)
            continue
        slot = current or RoutineSlot(
            school=school, academic_year=academic_year, section=section, weekday=weekday, period_id=period_id
        )
        slot.subject = values["subject"]
        slot.teacher = values.get("teacher")
        slot.room = values.get("room")
        planned.append(((weekday, period_id), slot))

    # Validate against the school's other sections and against each other.
    pending_teachers, pending_rooms = {}, {}
    for key, slot in planned:
        try:
            slot.full_clean(exclude=["school"])
        except ValidationError as exc:
            errors[key] = " ".join(message for messages in exc.message_dict.values() for message in messages)
            continue
        if slot.teacher_id:
            seen = pending_teachers.get((slot.weekday, slot.period_id, slot.teacher_id))
            if seen:
                errors[key] = f"{slot.teacher} is already used in this slot by another row of this form."
                continue
            pending_teachers[(slot.weekday, slot.period_id, slot.teacher_id)] = key
        if slot.room_id:
            seen = pending_rooms.get((slot.weekday, slot.period_id, slot.room_id))
            if seen:
                errors[key] = f"Room {slot.room} is already used in this slot by another row of this form."
                continue
            pending_rooms[(slot.weekday, slot.period_id, slot.room_id)] = key

    if errors:
        raise WeekGridError(errors)

    for slot in removals:
        slot.delete()
    for _key, slot in planned:
        slot.save()
    AuditLog.objects.create(
        school=school,
        user=user,
        action="routine.saved",
        description=f"{section}: {len(planned)} slot(s) set, {len(removals)} cleared",
    )
    return len(planned), len(removals)


class WeekGridError(Exception):
    """Carries per-cell messages so the editor can mark the offending cells."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)} timetable cell(s) could not be saved")


def room_utilisation(school, academic_year):
    """How many periods a week each room is in use, and how many sit empty."""
    from .models import Room

    counts = {}
    for slot in RoutineSlot.objects.filter(school=school, academic_year=academic_year, room__isnull=False):
        counts[slot.room_id] = counts.get(slot.room_id, 0) + 1
    slots_per_week = len(teaching_periods(school)) * len(WEEKDAYS)
    rows = []
    for room in Room.objects.filter(school=school).order_by("name"):
        used = counts.get(room.pk, 0)
        rows.append(
            {
                "room": room,
                "used": used,
                "capacity": room.capacity,
                "available": max(slots_per_week - used, 0),
                "percent": round(used * 100 / slots_per_week) if slots_per_week else None,
            }
        )
    return rows


def teacher_load(school, academic_year):
    """Periods a week per teacher, so nobody is quietly given a double load."""
    from employees.models import Employee

    counts = {}
    for slot in RoutineSlot.objects.filter(school=school, academic_year=academic_year, teacher__isnull=False):
        counts[slot.teacher_id] = counts.get(slot.teacher_id, 0) + 1
    rows = []
    for employee in Employee.objects.filter(
        school=school, employee_type=Employee.Type.TEACHER, status=Employee.Status.ACTIVE
    ).order_by("first_name", "last_name"):
        rows.append({"teacher": employee, "periods": counts.get(employee.pk, 0)})
    return sorted(rows, key=lambda row: -row["periods"])
