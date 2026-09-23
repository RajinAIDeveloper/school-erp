"""Each role's dashboard answers the question that role arrives with."""

from datetime import date, time
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from attendance.models import LeaveType, StudentAttendance
from attendance.services import save_register
from fees.services import collect_payment
from students.models import Enrollment, Student
from timetable.models import Period, RoutineSlot
from users.models import User

DAY = date(2026, 9, 21)


def test_manager_dashboard_reports_the_state_of_the_school(admin_client, erp, invoice):
    collect_payment(
        school=erp.school,
        user=erp.admin,
        invoice=invoice,
        amount=400,
        method="cash",
        reference="",
        date=DAY,
    )
    body = admin_client.get("/").content
    assert b"Active students" in body
    assert b"Class strength" in body
    assert b"Collected this month" in body
    assert b"Recent payments" in body
    assert b"Ayesha" in body


def test_manager_dashboard_surfaces_waiting_approvals(erp):
    from attendance.models import LeaveRequest

    leave_type = LeaveType.objects.create(school=erp.school, name="Casual", days_per_year=10)
    LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=leave_type,
        start_date=DAY,
        end_date=DAY,
        status="pending",
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/").content
    assert b"leave request" in body and b"waiting for you" in body


def test_teacher_dashboard_names_the_registers_still_missing(erp):
    from holidays.models import is_holiday

    client = Client()
    client.force_login(erp.teacher)
    body = client.get("/").content
    assert b"Registers" in body
    if is_holiday(erp.school, date.today()):
        # A closed day has no register to take, which the dashboard says instead of
        # listing sections as outstanding.
        assert b"closed today" in body
        return
    assert b"Take now" in body
    assert b"Class 1 - A" in body

    save_register(
        school=erp.school,
        user=erp.teacher,
        day=date.today(),
        entries=[(erp.enrollment, "present", "", None, None)],
    )
    assert b"Complete" in client.get("/").content


def test_teacher_dashboard_calls_a_half_taken_register_partial(erp):
    """One row out of two is not a register that has been taken."""
    from holidays.models import is_holiday

    if is_holiday(erp.school, date.today()):
        pytest.skip("the school is closed today, so no register is expected")
    other = Student.objects.create(
        school=erp.school,
        student_id="PART-1",
        first_name="Second",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school,
        student=other,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    save_register(
        school=erp.school,
        user=erp.teacher,
        day=date.today(),
        entries=[(erp.enrollment, "present", "", None, None)],
    )
    client = Client()
    client.force_login(erp.teacher)
    body = client.get("/").content
    assert b"1 of 2 recorded" in body
    assert b"Finish" in body
    assert b"Complete" not in body


def test_a_manager_sees_attendance_against_the_roll_not_the_rows(erp):
    """One child marked present in a class of three is 33 percent, not 100."""
    from holidays.models import is_holiday

    if is_holiday(erp.school, date.today()):
        pytest.skip("the school is closed today, so no register is expected")
    for roll in (2, 3):
        student = Student.objects.create(
            school=erp.school,
            student_id=f"ROLL-{roll}",
            first_name=f"Pupil {roll}",
            gender="M",
            date_of_birth=date(2016, 1, 1),
            admission_date=date(2026, 1, 1),
        )
        Enrollment.objects.create(
            school=erp.school,
            student=student,
            academic_year=erp.year,
            class_level=erp.level,
            section=erp.section,
            roll_number=roll,
        )
    save_register(
        school=erp.school,
        user=erp.admin,
        day=date.today(),
        entries=[(erp.enrollment, "present", "", None, None)],
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/").content.decode()
    assert "33%" in body
    assert "1 of 3 on the roll recorded" in body


def test_teacher_dashboard_lists_papers_still_to_mark(erp):
    client = Client()
    client.force_login(erp.teacher)
    body = client.get("/").content
    assert b"Marks still to enter" in body
    assert b"Term 1" in body
    assert b"0/1" in body


def test_teacher_dashboard_shows_todays_periods(erp):
    period = Period.objects.create(
        school=erp.school, name="Period 1", order=1, start_time=time(9), end_time=time(9, 45)
    )
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=date.today().isoweekday(),
        period=period,
        subject=erp.subject,
        teacher=erp.employee,
    )
    client = Client()
    client.force_login(erp.teacher)
    body = client.get("/").content
    assert b"My day" in body
    assert b"Period 1" in body


def test_accountant_dashboard_shows_takings_and_who_owes(erp, invoice):
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=300,
        method="mobile",
        reference="TX-1",
        date=date.today(),
    )
    client = Client()
    client.force_login(erp.accountant)
    body = client.get("/").content
    assert b"Collected today" in body
    assert b"Largest balances" in body
    assert b"Mobile" in body
    assert b"Ayesha" in body


def test_accountant_dashboard_does_not_leak_academic_panels(erp):
    client = Client()
    client.force_login(erp.accountant)
    body = client.get("/").content
    assert b"Marks still to enter" not in body
    assert b"Class strength" not in body


def test_staff_dashboard_is_their_own_record(erp):
    from employees.models import Employee

    LeaveType.objects.create(school=erp.school, name="Casual", days_per_year=10)
    Employee.objects.create(
        school=erp.school,
        user=erp.staff,
        employee_id="S40",
        employee_type="staff",
        first_name="Mine",
        gender="F",
        phone="01712345685",
        joining_date=date(2026, 1, 1),
    )
    client = Client()
    client.force_login(erp.staff)
    body = client.get("/").content
    assert b"My attendance" in body
    assert b"My leave" in body
    assert b"Casual" in body
    assert b"Class strength" not in body


def test_staff_dashboard_explains_an_unlinked_account(erp):
    user = User.objects.create_user("floating", school=erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name="Staff"))
    client = Client()
    client.force_login(user)
    body = client.get("/").content
    assert b"not linked to a staff record" in body


def test_families_land_on_their_own_records_at_home(erp):
    client = Client()
    client.force_login(erp.parent)
    response = client.get("/")
    assert response.status_code == 200
    assert b"Ayesha" in response.content
    assert b"Class strength" not in response.content


def test_manager_dashboard_stays_within_a_query_budget(admin_client, erp, django_assert_max_num_queries):
    for n in range(2, 20):
        student = Student.objects.create(
            school=erp.school,
            student_id=f"D{n}",
            first_name=f"Pupil {n}",
            gender="M",
            date_of_birth=date(2016, 1, 1),
            admission_date=date(2026, 1, 1),
        )
        enrollment = Enrollment.objects.create(
            school=erp.school,
            student=student,
            academic_year=erp.year,
            class_level=erp.level,
            section=erp.section,
            roll_number=n,
        )
        StudentAttendance.objects.create(school=erp.school, enrollment=enrollment, date=date.today(), status="present")
    # The panel is a fixed set of aggregates; rows must not add queries.
    with django_assert_max_num_queries(30):
        assert admin_client.get("/").status_code == 200


def test_dashboard_says_when_the_school_is_closed(erp):
    from holidays.models import Holiday

    Holiday.objects.create(school=erp.school, name="Closed", start_date=date.today(), end_date=date.today())
    client = Client()
    client.force_login(erp.admin)
    assert b"school is closed today" in client.get("/").content


def test_an_account_with_no_school_is_told_so(erp):
    user = User.objects.create_user("unassigned", password="Test-pass-9842")
    client = Client()
    client.force_login(user)
    response = client.get("/")
    assert response.status_code == 200
    assert b"No school configured" in response.content


def test_a_deactivated_school_stops_the_dashboard(erp):
    erp.school.is_active = False
    erp.school.save()
    client = Client()
    client.force_login(erp.admin)
    assert client.get("/").status_code == 403


def test_money_tiles_render_as_currency(admin_client, erp, invoice):
    collect_payment(
        school=erp.school,
        user=erp.admin,
        invoice=invoice,
        amount=Decimal("1000"),
        method="cash",
        reference="",
        date=date.today(),
    )
    body = admin_client.get("/").content.decode()
    assert "৳1,000.00" in body
