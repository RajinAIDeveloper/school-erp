from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client

from academics.models import AcademicYear
from employees.models import Employee
from examinations.services import publish_exam, save_mark
from fees.models import FeeConcession, FeeInvoice
from fees.services import generate_invoices
from holidays.models import Holiday
from users.models import User

SCREENS = {
    "dashboard": "/",
    "students": "/students/",
    "employees": "/employees/",
    "student_attendance": "/attendance/",
    "staff_attendance": "/attendance/staff/",
    "fees": "/fees/",
    "accounts": "/finance/",
    "results": "/exams/",
    "routine": "/routine/",
    "downloads": "/downloads/",
    "sms": "/sms/",
    "holidays": "/holidays/",
    "settings": "/settings/",
    "school_profile": "/settings/school/",
    "users": "/users/",
    "reports": "/reports/",
    "overview": "/reports/overview/",
}
ACCESS = {
    "Administrator": {
        "dashboard",
        "students",
        "employees",
        "student_attendance",
        "staff_attendance",
        "fees",
        "accounts",
        "results",
        "routine",
        "downloads",
        "sms",
        "holidays",
        "settings",
        "school_profile",
        "users",
        "reports",
        "overview",
    },
    "Principal": {
        "dashboard",
        "students",
        "employees",
        "student_attendance",
        "staff_attendance",
        "fees",
        "accounts",
        "results",
        "routine",
        "downloads",
        "sms",
        "holidays",
        "settings",
        "users",
        "reports",
        "overview",
    },
    "Accountant": {
        "dashboard",
        "students",
        "employees",
        "fees",
        "accounts",
        "downloads",
        "sms",
        "holidays",
        "settings",
        "reports",
        "overview",
    },
    "Teacher": {
        "dashboard",
        "students",
        # No "employees": the roster carries every colleague's contact details and NID.
        "student_attendance",
        "staff_attendance",
        "results",
        "routine",
        "downloads",
        "holidays",
        "settings",
        "reports",
    },
    "Staff": {"dashboard", "staff_attendance", "routine", "downloads", "holidays", "reports"},
    "Student": {"dashboard", "routine", "downloads", "holidays", "reports"},
    "Guardian": {"dashboard", "routine", "downloads", "holidays", "reports"},
}


@pytest.mark.parametrize("role", ACCESS)
def test_role_screen_matrix(erp, role):
    user = {
        "Administrator": erp.admin,
        "Teacher": erp.teacher,
        "Accountant": erp.accountant,
        "Staff": erp.staff,
        "Guardian": erp.parent,
    }.get(role)
    if not user:
        user = User.objects.create_user("role-" + role, school=erp.school, password="Test-pass-9842")
        user.groups.add(Group.objects.get(name=role))
    client = Client()
    client.force_login(user)
    for screen, url in SCREENS.items():
        actual = client.get(url).status_code
        expected = 200 if screen in ACCESS[role] else 403
        assert actual == expected, (role, screen, url, actual, expected)


def test_settings_hub_shows_only_the_cards_a_role_may_open(erp):
    """The hub is shared; what it offers is not."""
    c = Client()
    c.force_login(erp.teacher)
    body = c.get("/settings/").content
    assert b"Classes and sections" in body
    assert b"Chart of accounts" not in body
    assert b"SMS gateway" not in body
    assert b"Create defaults" not in body

    c.force_login(erp.accountant)
    body = c.get("/settings/").content
    assert b"Chart of accounts" in body
    assert b"Fees setup" in body
    assert b"SMS gateway" not in body

    c.force_login(erp.admin)
    body = c.get("/settings/").content
    assert b"SMS gateway" in body and b"Create defaults" in body


def test_settings_hub_is_closed_to_roles_with_no_setup_access(erp):
    c = Client()
    c.force_login(erp.parent)
    assert c.get("/settings/").status_code == 403


def test_initialise_defaults_fills_the_empty_dropdowns(erp):
    from fees.models import FeeCategory
    from timetable.models import Period

    c = Client()
    c.force_login(erp.admin)
    assert c.post("/settings/initialise/").status_code == 302
    assert Period.objects.filter(school=erp.school).count() >= 7
    assert FeeCategory.objects.filter(school=erp.school, name="Admission Fee").exists()
    tuition = FeeCategory.objects.get(school=erp.school, name="Tuition Fee")
    assert tuition.income_account.code == "4010"

    # Running it again changes nothing and says so.
    before = Period.objects.filter(school=erp.school).count()
    c.post("/settings/initialise/")
    assert Period.objects.filter(school=erp.school).count() == before


def test_initialise_defaults_is_administrator_only(erp):
    c = Client()
    c.force_login(erp.accountant)
    assert c.post("/settings/initialise/").status_code == 403


def test_principal_academic_setup_but_no_school_identity(erp):
    principal = User.objects.create_user("principal", school=erp.school, password="Test-pass-9842")
    principal.groups.add(Group.objects.get(name="Principal"))
    c = Client()
    c.force_login(principal)
    assert c.get("/settings/year/").status_code == 200
    assert c.get("/settings/").status_code == 200  # the hub, filtered to what they may open
    assert c.get("/settings/school/").status_code == 403
    assert c.get("/settings/sms/").status_code == 403
    assert c.get("/users/").status_code == 200
    assert c.get("/users/new/").status_code == 403


def test_teacher_can_access_assigned_student_and_marks_but_not_other(erp):
    other_user = User.objects.create_user("another-teacher", school=erp.school, password="Test-pass-9842")
    other_user.groups.add(Group.objects.get(name="Teacher"))
    c = Client()
    c.force_login(erp.teacher)
    assert c.get(f"/students/{erp.student.pk}/").status_code == 200
    assert c.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}").status_code == 200
    assert c.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.other_section.pk}").status_code == 200
    assert (
        b"Select a valid choice"
        in c.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.other_section.pk}").content
    )
    c.force_login(other_user)
    assert c.get(f"/students/{erp.student.pk}/").status_code == 404


def test_staff_attendance_is_own_and_leave_is_own(erp):
    from attendance.models import StaffAttendance

    Employee.objects.create(
        school=erp.school,
        employee_id="S2",
        employee_type="staff",
        first_name="Other",
        gender="M",
        phone="01712345670",
        joining_date=date(2026, 1, 1),
    )
    Employee.objects.create(
        school=erp.school,
        user=erp.staff,
        employee_id="S1",
        employee_type="staff",
        first_name="Mine",
        gender="M",
        phone="01712345671",
        joining_date=date(2026, 1, 1),
    )
    c = Client()
    c.force_login(erp.staff)
    r = c.get("/attendance/staff/?date=2026-09-21")
    assert r.status_code == 200
    assert b"Mine" in r.content and b"Other" not in r.content
    assert c.post("/attendance/staff/", {"date": "2026-09-21"}).status_code == 403


def test_student_and_guardian_portals_are_own(erp):
    from students.models import Student

    user = User.objects.create_user("learner", school=erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name="Student"))
    erp.student.user = user
    erp.student.save()
    for role_user in (user, erp.parent):
        c = Client()
        c.force_login(role_user)
        r = c.get("/")
        assert r.status_code == 200 and b"Ayesha" in r.content
        assert c.get(f"/students/{erp.student.pk}/").status_code == 200
        assert c.get("/reports/").status_code == 200
        assert c.get("/reports/overview/").status_code == 403
        assert c.get("/fees/").status_code == 403
        for route in ("/portal/", "/portal/attendance/", "/portal/fees/", "/portal/results/"):
            response = c.get(route)
            assert response.status_code == 200 and b"Ayesha" in response.content


def test_parent_portal_rejects_other_child_query_and_shows_only_published_results(erp, invoice):
    from attendance.models import StudentAttendance
    from examinations.models import ResultSnapshot
    from students.models import Student

    other = Student.objects.create(
        school=erp.school,
        student_id="PRIVATE",
        first_name="Private",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    StudentAttendance.objects.create(
        school=erp.school, enrollment=erp.enrollment, date=date(2026, 9, 21), status="present"
    )
    c = Client()
    c.force_login(erp.parent)
    assert c.get(f"/portal/fees/?student={other.pk}").status_code == 404
    assert c.get(f"/portal/results/?student={other.pk}").status_code == 404
    assert c.get("/portal/results/").status_code == 200
    assert b"Term 1" not in c.get("/portal/results/").content
    assert b"present" in c.get("/portal/attendance/?month=2026-09").content.lower()
    assert invoice.invoice_no.encode() in c.get("/portal/fees/").content
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    assert ResultSnapshot.objects.exists()
    assert b"Term 1" in c.get("/portal/results/").content


def test_teacher_cannot_open_parent_fee_portal(erp):
    c = Client()
    c.force_login(erp.teacher)
    assert c.get("/portal/fees/").status_code == 403


def test_reports_hub_shows_only_authorized_reports(erp):
    c = Client()
    c.force_login(erp.teacher)
    text = c.get("/reports/").content
    assert b"Student attendance" in text and b"Results and subject analysis" in text
    assert b"Trial balance" not in text and b"Fee collections" not in text
    c.force_login(erp.accountant)
    text = c.get("/reports/").content
    assert b"Fee collections" in text and b"Trial balance" in text
    assert b"Student attendance" not in text


def test_report_overview_counts_exports_and_permissions(erp):
    from fees.models import FeeInvoice
    from fees.services import collect_payment

    c = Client()
    c.force_login(erp.accountant)
    response = c.get("/reports/overview/?start=2026-09-01&end=2026-09-30")
    assert response.status_code == 200 and b"Active students" in response.content
    result = generate_invoices(
        school=erp.school,
        user=erp.accountant,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    assert result == (1, 0)
    inv = FeeInvoice.objects.get()
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=inv,
        amount=300,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    response = c.get("/reports/overview/?start=2026-09-01&end=2026-09-30&format=xlsx")
    assert response.status_code == 200
    from openpyxl import load_workbook

    ws = load_workbook(BytesIO(response.content)).active
    data = {r[0]: r[1] for r in list(ws.values)[1:]}
    assert data["Fee collected in range"] == 300 and data["Outstanding amount now"] == 700
    assert data["Ledger income in range"] == 300
    assert c.get("/reports/overview/?start=2026-09-30&end=2026-09-01").status_code == 200


def test_holiday_calendar_export_is_real_ical(erp):
    c = Client()
    c.force_login(erp.admin)
    Holiday.objects.create(
        school=erp.school, name="Autumn break", start_date=date(2026, 10, 1), end_date=date(2026, 10, 2)
    )
    r = c.get("/holidays/calendar.ics")
    assert r.status_code == 200
    assert b"DTSTART;VALUE=DATE:20261001" in r.content
    assert b"DTEND;VALUE=DATE:20261003" in r.content


def test_fee_discount_applied_during_generation(erp):
    FeeConcession.objects.create(school=erp.school, student=erp.student, percent=10, reason="Sibling")
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    assert FeeInvoice.objects.get().total == 900


def test_cross_school_settings_and_reports_denied(erp):
    foreign = User.objects.create_user("foreign", school=erp.other, password="Test-pass-9842")
    foreign.groups.add(Group.objects.get(name="Administrator"))
    c = Client()
    c.force_login(foreign)
    assert c.get("/reports/overview/?start=2026-09-01&end=2026-09-30").status_code == 200
    assert b"Ayesha" not in c.get("/").content
    assert b"Ayesha" not in c.get("/students/").content
    assert c.get(f"/students/{erp.student.pk}/").status_code == 404
