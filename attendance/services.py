import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school, sections_for
from core.models import AuditLog
from holidays.models import holiday_dates_between, is_holiday
from holidays.services import working_days
from messaging.notifications import notify_absences
from students.models import Enrollment

from .models import AttendanceStatus, StaffAttendance, StudentAttendance


def enrollments_for(section, day):
    return (
        Enrollment.objects.filter(
            school=section.school,
            section=section,
            academic_year__start_date__lte=day,
            academic_year__end_date__gte=day,
            student__admission_date__lte=day,
        )
        .exclude(status="left")
        .select_related("student", "academic_year")
    )


def roster_size(section, academic_year):
    """How many students a register for this section is expected to cover."""
    if academic_year is None:
        return 0
    return Enrollment.objects.filter(
        section=section,
        academic_year=academic_year,
        status=Enrollment.Status.ENROLLED,
        student__status="active",
    ).count()


def register_state(recorded, expected):
    """
    Complete, partial or missing.

    A register with one row out of thirty is not "taken"; a teacher who was interrupted
    needs to know that, and so does the head.
    """
    if not recorded:
        return "missing"
    if expected and recorded < expected:
        return "partial"
    return "complete"


def register_sections(user, school):
    """
    The sections whose daily register this person may take.

    Managers take any. Otherwise it depends on the school's choice: the class teacher only,
    or the class teacher and any teacher of the section, who can then cover.
    """
    from academics.models import Section
    from core.access import is_manager

    if is_manager(user):
        return Section.objects.filter(school=school)
    if school.register_takers == "class_teacher":
        employee = getattr(user, "employee_profile", None)
        if employee is None:
            return Section.objects.none()
        return Section.objects.filter(school=school, class_teacher=employee)
    return sections_for(user, school)


def register_closed(user, school, day):
    """Whether this day's register is past the window in which teachers may change it."""
    from core.access import is_manager

    days = school.register_edit_days
    return bool(days) and not is_manager(user) and (timezone.localdate() - day).days > days


@transaction.atomic
def save_register(*, school, user, day, entries, staff=False):
    assert_actor_school(user, school)
    if day > timezone.localdate() or is_holiday(school, day):
        raise ValidationError("Attendance cannot be recorded on future days, weekends or school holidays.")
    permission = "attendance.change_staffattendance" if staff else "attendance.change_studentattendance"
    if not user.has_perm(permission):
        raise PermissionDenied
    if register_closed(user, school, day):
        raise ValidationError(
            f"Registers older than {school.register_edit_days} day(s) can be changed only by the school's managers."
        )
    allowed = None if staff else set(register_sections(user, school).values_list("pk", flat=True))
    saved = 0
    absentees = []
    for obj, status, remarks, check_in, check_out in entries:
        assert_school(school, obj)
        if status not in AttendanceStatus.values:
            raise ValidationError("Invalid attendance status.")
        if not staff and obj.section_id not in allowed:
            raise PermissionDenied("This section's register is taken by its class teacher.")
        if staff and check_in and check_out and check_out <= check_in:
            raise ValidationError("Check-out must be after check-in.")
        if not staff and not (obj.academic_year.start_date <= day <= obj.academic_year.end_date):
            raise ValidationError("Attendance date is outside the enrollment year.")
        model = StaffAttendance if staff else StudentAttendance
        key = {"employee": obj} if staff else {"enrollment": obj}
        defaults = {"school": school, "status": status, "remarks": remarks, "recorded_by": user}
        if staff:
            defaults.update(check_in=check_in, check_out=check_out)
        record, _ = model.objects.update_or_create(**key, date=day, defaults=defaults)
        AuditLog.objects.create(
            school=school,
            user=user,
            action="attendance.saved",
            model=record._meta.label,
            object_id=str(record.pk),
            description=f"{day}: {status}",
        )
        if not staff and status == AttendanceStatus.ABSENT:
            absentees.append(obj)
        saved += 1
    if absentees:
        # Queued only; the worker sends. A gateway problem must not undo the register.
        transaction.on_commit(lambda: notify_absences(school, day, absentees))
    return saved


def monthly_matrix(school, objects, year, month, staff=False):
    today = timezone.localdate()
    days = [date(year, month, d) for d in range(1, calendar.monthrange(year, month)[1] + 1)]
    model = StaffAttendance if staff else StudentAttendance
    field = "employee_id" if staff else "enrollment_id"
    records = {
        (getattr(a, field), a.date): a.status
        for a in model.objects.filter(
            school=school, date__year=year, date__month=month, **{field + "__in": [o.pk for o in objects]}
        )
    }
    # One holiday query for the whole month instead of one per person per day.
    closed = holiday_dates_between(school, days[0], days[-1])
    weekend = school.weekend_day_numbers
    open_days = [d for d in days if d not in closed and d.isoweekday() not in weekend]
    rows = []
    for obj in objects:
        start = obj.joining_date if staff else max(obj.student.admission_date, obj.academic_year.start_date)
        end = today if staff else min(today, obj.academic_year.end_date)
        working = [d for d in open_days if start <= d <= end]
        present = sum(records.get((obj.pk, d)) in ("present", "late") for d in working)
        rows.append(
            {
                "name": str(obj) if staff else obj.student.full_name,
                "object": obj,
                "cells": [records.get((obj.pk, d), "?") for d in days],
                "present": present,
                "working": len(working),
                "pct": round(present * 100 / len(working), 1) if working else None,
            }
        )
    return days, rows


# ----------------------------------------------------------------------------- leave


def leave_days(school, start, end):
    """
    How many days a leave request costs: the school's working days in the range.

    This is the one definition used everywhere leave is counted: the request form, the
    balance, the register rows an approval creates, and the leave register report. A
    request from Thursday to Sunday costs two days at a school closed on Friday and
    Saturday, not four.
    """
    return working_days(school, start, end)


def leave_days_in_year(school, start, end, year):
    """The part of a request that falls inside one calendar year, in working days."""
    window_start = max(start, date(year, 1, 1))
    window_end = min(end, date(year, 12, 31))
    if window_end < window_start:
        return 0
    return leave_days(school, window_start, window_end)


def leave_balance(employee, year):
    """
    Entitlement, days already approved and what is left, per leave type.

    Schools run entitlement by calendar year, and an approved request is counted from the
    day it is approved rather than when it is taken, so staff cannot book past a quota.
    A request that straddles New Year charges each year only for the days inside it.
    """
    from .models import LeaveRequest, LeaveType

    counted = {}
    for request in LeaveRequest.objects.filter(
        employee=employee,
        status=LeaveRequest.Status.APPROVED,
        start_date__year__lte=year,
        end_date__year__gte=year,
    ):
        counted[request.leave_type_id] = counted.get(request.leave_type_id, 0) + request.days_in_year(year)
    rows = []
    for leave_type in LeaveType.objects.filter(school=employee.school):
        taken = counted.get(leave_type.pk, 0)
        rows.append(
            {
                "leave_type": leave_type,
                "entitlement": leave_type.days_per_year,
                "used": taken,
                "remaining": leave_type.days_per_year - taken,
                "allow_negative": leave_type.allow_negative,
            }
        )
    return rows


def overlapping_leave(employee, start, end, exclude_pk=None):
    from .models import LeaveRequest

    qs = LeaveRequest.objects.filter(
        employee=employee,
        status__in=[LeaveRequest.Status.PENDING, LeaveRequest.Status.APPROVED],
        start_date__lte=end,
        end_date__gte=start,
    )
    return qs.exclude(pk=exclude_pk) if exclude_pk else qs


@dataclass
class LeaveApplied:
    """What approving a request did to the register, so the screen can say so."""

    created: int = 0
    skipped: list = field(default_factory=list)  # dates that already had a manual entry

    @property
    def summary(self):
        text = f"{self.created} working day(s) marked as leave on the register."
        if self.skipped:
            days = ", ".join(f"{day:%d %b}" for day in self.skipped)
            text += f" {len(self.skipped)} day(s) already had an attendance entry and were left as they are: {days}."
        return text


@transaction.atomic
def apply_leave(leave_request, user):
    """
    Turn an approved request into attendance rows so registers, reports and any payroll
    rule see the same truth. Holidays and weekends inside the range are skipped.

    A day that already has a register entry is never touched: someone recorded that the
    person was in, late or absent, and a leave approval must not quietly rewrite it. Such
    days are reported back rather than overwritten, and they stay outside the request, so
    withdrawing the approval later cannot delete them either.
    """

    from .models import StaffAttendance

    school = leave_request.school
    closed = holiday_dates_between(school, leave_request.start_date, leave_request.end_date)
    weekend = school.weekend_day_numbers
    existing = {
        row.date: row
        for row in StaffAttendance.objects.filter(
            employee=leave_request.employee,
            date__range=(leave_request.start_date, leave_request.end_date),
        )
    }
    outcome = LeaveApplied()
    day = leave_request.start_date
    while day <= leave_request.end_date:
        if day not in closed and day.isoweekday() not in weekend:
            current = existing.get(day)
            if current is None:
                StaffAttendance.objects.create(
                    school=school,
                    employee=leave_request.employee,
                    date=day,
                    status=AttendanceStatus.LEAVE,
                    remarks=f"{leave_request.leave_type} (approved)",
                    recorded_by=user,
                    created_by_leave=leave_request,
                )
                outcome.created += 1
            elif current.created_by_leave_id != leave_request.pk:
                outcome.skipped.append(day)
        day += timedelta(days=1)
    AuditLog.objects.create(
        school=school,
        user=user,
        action="leave.applied",
        model=leave_request._meta.label,
        object_id=str(leave_request.pk),
        description=outcome.summary,
    )
    return outcome


@transaction.atomic
def withdraw_leave(leave_request, user):
    """Undo only the attendance rows this request created, leaving manual entries alone."""
    from .models import StaffAttendance

    removed, _ = StaffAttendance.objects.filter(created_by_leave=leave_request).delete()
    AuditLog.objects.create(
        school=leave_request.school,
        user=user,
        action="leave.withdrawn",
        model=leave_request._meta.label,
        object_id=str(leave_request.pk),
        description=f"{removed} leave attendance day(s) removed",
    )
    return removed


@transaction.atomic
def self_check(employee, user, when=None):
    """Staff self check-in, then check-out on a second press. Policy gated by the school."""
    from .models import StaffAttendance

    school = employee.school
    if not school.staff_self_checkin:
        raise ValidationError("Self check-in is switched off for this school.")
    today = timezone.localdate()
    if is_holiday(school, today):
        raise ValidationError("The school is closed today.")
    now = (when or timezone.localtime()).time().replace(microsecond=0)
    row, created = StaffAttendance.objects.get_or_create(
        employee=employee,
        date=today,
        defaults={"school": school, "status": AttendanceStatus.PRESENT, "check_in": now, "recorded_by": user},
    )
    if not created:
        if row.check_in and row.check_out:
            raise ValidationError("You have already checked in and out today.")
        if row.check_in and now <= row.check_in:
            raise ValidationError("Check-out must be after check-in.")
        row.check_out = now if row.check_in else row.check_out
        row.check_in = row.check_in or now
        row.recorded_by = user
        row.save(update_fields=["check_in", "check_out", "recorded_by", "updated_at"])
    AuditLog.objects.create(
        school=school,
        user=user,
        action="attendance.self_check",
        model=row._meta.label,
        object_id=str(row.pk),
        description=f"in {row.check_in} out {row.check_out or '-'}",
    )
    return row
