import calendar
from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school, sections_for
from core.models import AuditLog
from holidays.models import is_holiday
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


@transaction.atomic
def save_register(*, school, user, day, entries, staff=False):
    assert_actor_school(user, school)
    if day > timezone.localdate() or is_holiday(school, day):
        raise ValidationError("Attendance cannot be recorded on future days, weekends or school holidays.")
    permission = "attendance.change_staffattendance" if staff else "attendance.change_studentattendance"
    if not user.has_perm(permission):
        raise PermissionDenied
    saved = 0
    for obj, status, remarks, check_in, check_out in entries:
        assert_school(school, obj)
        if status not in AttendanceStatus.values:
            raise ValidationError("Invalid attendance status.")
        if not staff and not sections_for(user, school).filter(pk=obj.section_id).exists():
            raise PermissionDenied
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
        saved += 1
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
    rows = []
    for obj in objects:
        start = obj.joining_date if staff else max(obj.student.admission_date, obj.academic_year.start_date)
        end = today if staff else min(today, obj.academic_year.end_date)
        working = [d for d in days if start <= d <= end and not is_holiday(school, d)]
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
