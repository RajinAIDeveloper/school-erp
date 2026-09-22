"""Registers, monthly reports, per-student history and absence alerts."""

from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from attendance.models import StaffAttendance, StudentAttendance
from attendance.services import apply_leave, monthly_matrix, save_register, self_check, withdraw_leave
from holidays.models import Holiday
from messaging.models import SMSMessage
from students.models import Enrollment, Student

DAY = date(2026, 9, 21)  # a Monday


def register_payload(erp, status="present"):
    return {
        "date": DAY.isoformat(),
        "section": erp.section.pk,
        f"{erp.enrollment.pk}-status": status,
        f"{erp.enrollment.pk}-remarks": "",
    }


def test_register_saves_from_the_screen(admin_client, erp):
    response = admin_client.post("/attendance/", register_payload(erp))
    assert response.status_code == 302
    assert StudentAttendance.objects.get(enrollment=erp.enrollment, date=DAY).status == "present"


def test_register_shows_radio_options_and_mark_all_controls(admin_client, erp):
    body = admin_client.get(f"/attendance/?date={DAY}&section={erp.section.pk}").content
    assert b'data-mark-all="present"' in body
    assert b'type="radio"' in body
    assert b"Ayesha" in body


def test_monthly_matrix_query_count_does_not_grow_with_rows(erp, django_assert_max_num_queries):
    enrollments = [erp.enrollment]
    for n in range(2, 16):
        student = Student.objects.create(
            school=erp.school,
            student_id=f"M{n}",
            first_name=f"Pupil {n}",
            gender="M",
            date_of_birth=date(2016, 1, 1),
            admission_date=date(2026, 1, 1),
        )
        enrollments.append(
            Enrollment.objects.create(
                school=erp.school,
                student=student,
                academic_year=erp.year,
                class_level=erp.level,
                section=erp.section,
                roll_number=n,
            )
        )
    Holiday.objects.create(school=erp.school, name="Break", start_date=date(2026, 9, 10), end_date=date(2026, 9, 12))
    # One attendance query and one holiday query, whatever the size of the class.
    with django_assert_max_num_queries(4):
        _days, rows = monthly_matrix(erp.school, enrollments, 2026, 9)
    assert len(rows) == 15


def test_monthly_matrix_excludes_weekends_and_holidays(erp):
    Holiday.objects.create(school=erp.school, name="Autumn", start_date=date(2026, 9, 1), end_date=date(2026, 9, 7))
    _days, rows = monthly_matrix(erp.school, [erp.enrollment], 2026, 9)
    working = rows[0]["working"]
    assert working > 0
    # September 2026 has 30 days: 7 are holiday, and Fridays and Saturdays are weekends.
    assert working < 23


def test_student_history_is_scoped_to_what_the_viewer_may_see(erp):
    StudentAttendance.objects.create(school=erp.school, enrollment=erp.enrollment, date=DAY, status="absent")
    other = Student.objects.create(
        school=erp.school,
        student_id="X1",
        first_name="Hidden",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    client = Client()
    client.force_login(erp.teacher)
    assert client.get(f"/attendance/student/{erp.student.pk}/?month=2026-09").status_code == 200
    assert client.get(f"/attendance/student/{other.pk}/?month=2026-09").status_code == 404


def test_student_history_handles_a_nonsense_month(admin_client, erp):
    response = admin_client.get(f"/attendance/student/{erp.student.pk}/?month=not-a-month")
    assert response.status_code == 200


def test_daily_summary_counts_and_flags_missing_registers(admin_client, erp):
    body = admin_client.get(f"/attendance/summary/?date={DAY}").content
    assert b"Not taken" in body
    save_register(
        school=erp.school,
        user=erp.admin,
        day=DAY,
        entries=[(erp.enrollment, "present", "", None, None)],
    )
    body = admin_client.get(f"/attendance/summary/?date={DAY}").content
    assert b"Taken" in body


def test_absence_alert_is_queued_once_per_student_and_day(erp, django_capture_on_commit_callbacks):
    erp.school.notify_absence_sms = True
    erp.school.save()
    entries = [(erp.enrollment, "absent", "", None, None)]
    # Alerts are queued on commit so a gateway fault can never undo a saved register.
    for _ in range(2):
        with django_capture_on_commit_callbacks(execute=True):
            save_register(school=erp.school, user=erp.admin, day=DAY, entries=entries)
    messages = SMSMessage.objects.filter(school=erp.school)
    assert messages.count() == 1
    message = messages.get()
    assert message.status == "queued"
    assert "Ayesha" in message.body
    assert message.dedupe_key == f"absence:{erp.enrollment.pk}:{DAY.isoformat()}"


def test_no_absence_alert_when_the_school_has_not_enabled_it(erp, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        save_register(
            school=erp.school,
            user=erp.admin,
            day=DAY,
            entries=[(erp.enrollment, "absent", "", None, None)],
        )
    assert not SMSMessage.objects.exists()


def test_absence_alert_respects_a_family_opting_out(erp, django_capture_on_commit_callbacks):
    erp.school.notify_absence_sms = True
    erp.school.save()
    erp.guardian.sms_opt_in = False
    erp.guardian.save()
    with django_capture_on_commit_callbacks(execute=True):
        save_register(
            school=erp.school,
            user=erp.admin,
            day=DAY,
            entries=[(erp.enrollment, "absent", "", None, None)],
        )
    assert not SMSMessage.objects.exists()


def test_present_students_are_never_texted(erp, django_capture_on_commit_callbacks):
    erp.school.notify_absence_sms = True
    erp.school.save()
    with django_capture_on_commit_callbacks(execute=True):
        save_register(
            school=erp.school,
            user=erp.admin,
            day=DAY,
            entries=[(erp.enrollment, "present", "", None, None)],
        )
    assert not SMSMessage.objects.exists()


def test_approved_leave_marks_attendance_and_withdrawal_removes_only_those_rows(erp):
    from attendance.models import LeaveRequest, LeaveType

    leave_type = LeaveType.objects.create(school=erp.school, name="Casual", days_per_year=10)
    # Monday to Wednesday, with a holiday on the Tuesday.
    Holiday.objects.create(
        school=erp.school, name="Civic day", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
    )
    manual = StaffAttendance.objects.create(
        school=erp.school, employee=erp.employee, date=date(2026, 9, 18), status="present"
    )
    leave = LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=leave_type,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 23),
        status="approved",
    )
    created = apply_leave(leave, erp.admin)
    assert created == 2  # the holiday in the middle is skipped
    assert StaffAttendance.objects.filter(created_by_leave=leave, status="leave").count() == 2

    removed = withdraw_leave(leave, erp.admin)
    assert removed == 2
    assert StaffAttendance.objects.filter(pk=manual.pk).exists()


def test_self_check_in_then_out_requires_the_school_policy(erp):
    """Uses a pinned clock: the real one would make this fail near midnight."""
    from unittest import mock

    from django.utils import timezone

    erp.employee.user = erp.teacher
    erp.employee.save()
    with pytest.raises(ValidationError):
        self_check(erp.employee, erp.teacher)

    erp.school.staff_self_checkin = True
    erp.school.save()
    arrival = timezone.localtime().replace(hour=8, minute=30, second=0, microsecond=0)
    with mock.patch("attendance.services.timezone.localtime", return_value=arrival):
        row = self_check(erp.employee, erp.teacher)
    assert row.check_in == arrival.time() and row.check_out is None

    departure = arrival + timedelta(hours=8)
    row = self_check(erp.employee, erp.teacher, when=departure)
    assert row.check_out == departure.time()

    with pytest.raises(ValidationError):
        self_check(erp.employee, erp.teacher, when=departure)


def test_self_check_in_refused_on_a_closed_day(erp):
    from django.utils import timezone

    erp.school.staff_self_checkin = True
    erp.school.save()
    erp.employee.user = erp.teacher
    erp.employee.save()
    today = timezone.localdate()
    Holiday.objects.create(school=erp.school, name="Closed", start_date=today, end_date=today)
    with pytest.raises(ValidationError):
        self_check(erp.employee, erp.teacher)


def test_register_still_refuses_holidays_and_future_dates(erp):
    from django.utils import timezone

    Holiday.objects.create(school=erp.school, name="Closed", start_date=DAY, end_date=DAY)
    with pytest.raises(ValidationError):
        save_register(
            school=erp.school,
            user=erp.admin,
            day=DAY,
            entries=[(erp.enrollment, "present", "", None, None)],
        )
    with pytest.raises(ValidationError):
        save_register(
            school=erp.school,
            user=erp.admin,
            day=timezone.localdate() + timedelta(days=1),
            entries=[(erp.enrollment, "present", "", None, None)],
        )
