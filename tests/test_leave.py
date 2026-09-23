"""Leave requests: entitlement, overlap, approval side effects and self check-in."""

from datetime import date

from django.test import Client

from attendance.models import LeaveRequest, LeaveType, StaffAttendance
from attendance.views import LeaveForm
from core.models import AuditLog
from employees.models import Employee
from holidays.models import Holiday


def leave_type(erp, name="Casual", days=10):
    return LeaveType.objects.create(school=erp.school, name=name, days_per_year=days)


def request_payload(erp, kind, start="2026-09-21", end="2026-09-23"):
    return {
        "employee": erp.employee.pk,
        "leave_type": kind.pk,
        "start_date": start,
        "end_date": end,
        "reason": "Family matter",
    }


def test_leave_form_rejects_an_overlapping_request(erp):
    kind = leave_type(erp)
    LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 23),
        status="pending",
    )
    form = LeaveForm(request_payload(erp, kind, "2026-09-22", "2026-09-25"), school=erp.school)
    assert not form.is_valid()
    assert "already has leave" in " ".join(form.non_field_errors())


def test_leave_form_rejects_going_past_the_entitlement(erp):
    kind = leave_type(erp, days=2)
    # 21 to 25 September 2026 is Monday to Friday; Friday is a weekend day here, so the
    # request costs four working days against an entitlement of two.
    form = LeaveForm(request_payload(erp, kind, "2026-09-21", "2026-09-25"), school=erp.school)
    assert not form.is_valid()
    assert "remain in 2026" in " ".join(form.errors["leave_type"])


def test_leave_form_rejects_an_end_before_the_start(erp):
    kind = leave_type(erp)
    form = LeaveForm(request_payload(erp, kind, "2026-09-25", "2026-09-21"), school=erp.school)
    assert not form.is_valid()
    assert "end_date" in form.errors


def test_leave_form_accepts_a_clean_request(erp):
    kind = leave_type(erp)
    form = LeaveForm(request_payload(erp, kind), school=erp.school)
    assert form.is_valid(), form.errors


def test_approving_marks_the_register_and_withdrawing_clears_it(erp):
    kind = leave_type(erp)
    Holiday.objects.create(
        school=erp.school, name="Civic day", start_date=date(2026, 9, 22), end_date=date(2026, 9, 22)
    )
    leave = LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 23),
    )
    client = Client()
    client.force_login(erp.admin)

    response = client.post(f"/attendance/leave/{leave.pk}/review/", {"status": "approved", "note": "Fine"})
    assert response.status_code == 302
    leave.refresh_from_db()
    assert leave.status == "approved" and leave.reviewed_by == erp.admin and leave.review_note == "Fine"
    # Two working days; the holiday in the middle is not marked.
    assert StaffAttendance.objects.filter(created_by_leave=leave, status="leave").count() == 2

    client.post(f"/attendance/leave/{leave.pk}/review/", {"status": "rejected", "note": "Withdrawn"})
    leave.refresh_from_db()
    assert leave.status == "rejected"
    assert not StaffAttendance.objects.filter(created_by_leave=leave).exists()
    assert AuditLog.objects.filter(action="leave.withdrawn").exists()


def test_rejecting_a_pending_request_touches_no_attendance(erp):
    kind = leave_type(erp)
    leave = LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 22),
    )
    client = Client()
    client.force_login(erp.admin)
    client.post(f"/attendance/leave/{leave.pk}/review/", {"status": "rejected"})
    leave.refresh_from_db()
    assert leave.status == "rejected"
    assert not StaffAttendance.objects.exists()


def test_a_teacher_cannot_decide_their_own_request(erp):
    kind = leave_type(erp)
    leave = LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 22),
    )
    client = Client()
    client.force_login(erp.teacher)
    assert client.post(f"/attendance/leave/{leave.pk}/review/", {"status": "approved"}).status_code == 403
    leave.refresh_from_db()
    assert leave.status == "pending"


def test_leave_list_shows_own_requests_only_to_the_requester(erp):
    kind = leave_type(erp)
    colleague = Employee.objects.create(
        school=erp.school,
        employee_id="S20",
        first_name="Colleague",
        gender="M",
        phone="01712345620",
        joining_date=date(2026, 1, 1),
    )
    LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 22),
    )
    LeaveRequest.objects.create(
        school=erp.school,
        employee=colleague,
        leave_type=kind,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 22),
    )
    client = Client()
    client.force_login(erp.teacher)
    body = client.get("/attendance/leave/").content
    assert b"Colleague" not in body
    assert b"Leave balance" in body or b"Casual" in body

    client.force_login(erp.admin)
    assert b"Colleague" in client.get("/attendance/leave/").content


def test_leave_list_filters_by_status_and_shows_the_pending_count(erp):
    kind = leave_type(erp)
    LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=date(2026, 9, 21),
        end_date=date(2026, 9, 22),
        status="pending",
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/attendance/leave/?status=pending").content
    assert b"waiting for a decision" in body
    assert b"Family" not in client.get("/attendance/leave/?status=approved").content


def test_self_check_in_button_appears_only_under_the_policy(erp):
    erp.employee.user = erp.teacher
    erp.employee.save()
    client = Client()
    client.force_login(erp.teacher)
    assert b"Check in / out" not in client.get("/attendance/staff/?date=2026-09-21").content

    erp.school.staff_self_checkin = True
    erp.school.save()
    assert b"Check in / out" in client.get("/attendance/staff/?date=2026-09-21").content


def test_self_check_in_endpoint_records_arrival_then_departure(erp):
    from datetime import timedelta
    from unittest import mock

    from django.utils import timezone

    erp.employee.user = erp.teacher
    erp.employee.save()
    erp.school.staff_self_checkin = True
    erp.school.save()
    client = Client()
    client.force_login(erp.teacher)

    arrival = timezone.localtime().replace(hour=8, minute=30, second=0, microsecond=0)
    with mock.patch("attendance.services.timezone.localtime", return_value=arrival):
        assert client.post("/attendance/staff/check-in/").status_code == 302
    row = StaffAttendance.objects.get(employee=erp.employee)
    assert row.check_in == arrival.time() and row.status == "present"

    # Checking out in the same second is refused, as a real departure never is.
    with mock.patch("attendance.services.timezone.localtime", return_value=arrival):
        client.post("/attendance/staff/check-in/")
    row.refresh_from_db()
    assert row.check_out is None

    with mock.patch("attendance.services.timezone.localtime", return_value=arrival + timedelta(hours=8)):
        client.post("/attendance/staff/check-in/")
    row.refresh_from_db()
    assert row.check_out == (arrival + timedelta(hours=8)).time()


def test_self_check_in_needs_a_linked_employee(erp):
    erp.school.staff_self_checkin = True
    erp.school.save()
    client = Client()
    client.force_login(erp.parent)
    assert client.post("/attendance/staff/check-in/").status_code == 404
