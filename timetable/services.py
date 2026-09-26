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

from .models import WEEKDAYS, Period, PeriodDayTime, RoutineSlot, day_times, school_weekdays, times_on


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

    weekdays = school_weekdays(school)
    timings = day_times(school)
    rows = []
    for period in periods:
        cells = []
        for weekday, label in weekdays:
            times = times_on(period, weekday, timings)
            cells.append(
                {
                    "weekday": weekday,
                    "label": label,
                    "off": times is None,
                    # Shown in the cell only when the day keeps other times than the period's own.
                    "times": times if times and times != (period.start_time, period.end_time) else None,
                    "slots": sorted(by_cell.get((period.pk, weekday), []), key=lambda s: str(s.section)),
                }
            )
        rows.append({"period": period, "cells": cells})
    return {"periods": periods, "weekdays": weekdays, "rows": rows}


def free_teachers(school, academic_year, weekday, period, employee_type="teacher"):
    """
    Who is not already teaching in this slot.

    Busy means busy at that time, not merely booked into the same named period: schools
    run overlapping timetables (a laboratory double, a shortened assembly day), and a
    teacher in a lesson from 9:00 to 9:45 is not free for one starting at 9:30.
    """
    from employees.models import Employee

    timings = day_times(school)
    wanted = times_on(period, weekday, timings)
    busy = []
    if wanted is not None:
        for slot in RoutineSlot.objects.filter(
            school=school, academic_year=academic_year, weekday=weekday, teacher__isnull=False
        ).select_related("period"):
            theirs = times_on(slot.period, weekday, timings)
            if theirs and theirs[0] < wanted[1] and wanted[0] < theirs[1]:
                busy.append(slot.teacher_id)
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
    # Only the days the school meets, and only the periods held on each of them.
    timings = day_times(school)
    slots_per_week = sum(
        1
        for period in teaching_periods(school)
        for weekday, _label in school_weekdays(school)
        if times_on(period, weekday, timings) is not None
    )
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


# ------------------------------------------------------------------ the school day


def ordinal(number):
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _add(clock, minutes):
    from datetime import datetime, timedelta

    start = datetime(2000, 1, 1, clock.hour, clock.minute)
    moved = start + timedelta(minutes=minutes)
    if moved.date() != start.date():
        raise ValidationError("The day would run past midnight. Start earlier or make the periods shorter.")
    return moved.time()


def plan_day(first_start, minutes, count, breaks=()):
    """
    The periods of a day, built from when it starts, how long a period lasts and how many
    there are. `breaks` is [(after which period, minutes, name)]. Returns
    [(name, start, end, is_break)] in order.
    """
    if not 1 <= count <= 15:
        raise ValidationError("Give between 1 and 15 periods.")
    if not 10 <= minutes <= 180:
        raise ValidationError("A period lasts between 10 and 180 minutes.")
    after = {}
    for position, length, name in breaks:
        if not 1 <= position < count:
            raise ValidationError(f"A break comes after one of periods 1 to {count - 1}.")
        if not 5 <= length <= 120:
            raise ValidationError("A break lasts between 5 and 120 minutes.")
        if position in after:
            raise ValidationError("Two breaks cannot come after the same period.")
        after[position] = (length, (name or "").strip()[:50] or "Break")
    plan, clock = [], first_start
    for number in range(1, count + 1):
        end = _add(clock, minutes)
        plan.append((f"{ordinal(number)} period", clock, end, False))
        clock = end
        if number in after:
            length, name = after[number]
            end = _add(clock, length)
            plan.append((name, clock, end, True))
            clock = end
    return plan


@transaction.atomic
def build_day(*, school, user, plan):
    """
    Replace the school's periods with a planned day. Refused once any period is used in a
    routine: removing it would take those lessons with it. Such periods are changed one by one.
    """
    assert_actor_school(user, school)
    if not (user.has_perm("timetable.add_period") and user.has_perm("timetable.delete_period")):
        raise PermissionDenied
    existing = Period.objects.filter(school=school)
    if RoutineSlot.objects.filter(period__in=existing).exists():
        raise ValidationError(
            "The periods are already used in a routine. Change them one by one, so no lesson is lost."
        )
    existing.delete()
    made = [
        Period.objects.create(school=school, name=name, order=order, start_time=start, end_time=end, is_break=is_break)
        for order, (name, start, end, is_break) in enumerate(plan, start=1)
    ]
    AuditLog.objects.create(
        school=school,
        user=user,
        action="routine.day_built",
        description=f"{len(made)} period(s) from {plan[0][1]:%H:%M} to {plan[-1][2]:%H:%M}",
    )
    return made


@transaction.atomic
def save_day_times(*, school, user, weekday, rows):
    """
    Set one day's own timings. `rows` maps each period to (start, end, not held). A period at
    its usual times keeps no separate timing. The periods held that day must not overlap.
    """
    assert_actor_school(user, school)
    if not user.has_perm("timetable.change_period"):
        raise PermissionDenied
    if weekday not in dict(WEEKDAYS):
        raise ValidationError("Choose a day.")
    held = []
    for period, (start, end, not_held) in rows.items():
        assert_school(school, period)
        if not_held:
            continue
        if start is None or end is None or end <= start:
            raise ValidationError(f"{period.name}: give a start and an end after it, or mark it not held.")
        held.append((start, end, period))
    held.sort(key=lambda item: (item[0], item[2].order))
    for (_start, first_end, first), (second_start, _end, second) in zip(held, held[1:], strict=False):
        if second_start < first_end:
            raise ValidationError(f"{first.name} and {second.name} overlap on this day.")
    changed = 0
    for period, (start, end, not_held) in rows.items():
        usual = not not_held and (start, end) == (period.start_time, period.end_time)
        current = PeriodDayTime.objects.filter(period=period, weekday=weekday).first()
        if usual:
            if current:
                current.delete()
                changed += 1
            continue
        values = {
            "start_time": None if not_held else start,
            "end_time": None if not_held else end,
            "not_held": bool(not_held),
        }
        if current is None:
            PeriodDayTime.objects.create(school=school, period=period, weekday=weekday, **values)
            changed += 1
        elif (current.start_time, current.end_time, current.not_held) != tuple(values.values()):
            PeriodDayTime.objects.filter(pk=current.pk).update(**values)
            changed += 1
    if changed:
        AuditLog.objects.create(
            school=school,
            user=user,
            action="routine.day_times",
            description=f"{dict(WEEKDAYS)[weekday]}: {changed} period(s) changed",
        )
    return changed


def stray_slots(school):
    """Lessons set on a day the school no longer meets, or in a period no longer held that day."""
    weekend = school.weekend_day_numbers
    timings = day_times(school)
    return [
        slot
        for slot in RoutineSlot.objects.filter(school=school).select_related("period", "section__class_level")
        if slot.weekday in weekend or times_on(slot.period, slot.weekday, timings) is None
    ]


@transaction.atomic
def clear_stray_slots(*, school, user):
    assert_actor_school(user, school)
    if not user.has_perm("timetable.delete_routineslot"):
        raise PermissionDenied
    stray = stray_slots(school)
    RoutineSlot.objects.filter(pk__in=[slot.pk for slot in stray]).delete()
    if stray:
        AuditLog.objects.create(
            school=school,
            user=user,
            action="routine.stray_cleared",
            description=f"{len(stray)} lesson(s) on days or periods the school no longer holds",
        )
    return len(stray)
