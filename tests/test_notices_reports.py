"""Notices, and the reports hub each role is offered."""

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import Group
from django.test import Client

from academics.models import ClassLevel
from core.models import AuditLog
from downloads.models import Notice, live_notices_for
from students.models import Student
from users.models import User


def notice(erp, **overrides):
    values = dict(
        school=erp.school,
        title="Sports day on Thursday",
        body="Assembly at 8am. Students may wear house colours.",
        audience="everyone",
        publish_at=date.today(),
    )
    values.update(overrides)
    return Notice.objects.create(**values)


def test_a_notice_is_live_only_between_its_dates(erp):
    future = notice(erp, title="Next term", publish_at=date.today() + timedelta(days=7))
    expired = notice(
        erp,
        title="Last month",
        publish_at=date.today() - timedelta(days=40),
        expires_at=date.today() - timedelta(days=10),
    )
    current = notice(erp)

    assert current.is_live and not future.is_live and not expired.is_live
    live = live_notices_for(erp.admin, erp.school)
    assert current in live and future not in live and expired not in live


def test_pinned_notices_come_first(erp):
    notice(erp, title="Ordinary")
    notice(erp, title="Important", is_pinned=True)
    assert live_notices_for(erp.admin, erp.school)[0].title == "Important"


def test_staff_only_notices_are_hidden_from_families(erp):
    notice(erp, title="Staff meeting", audience="staff")
    notice(erp, title="Sports day")

    family = [n.title for n in live_notices_for(erp.parent, erp.school)]
    assert "Sports day" in family
    assert "Staff meeting" not in family

    staffroom = [n.title for n in live_notices_for(erp.teacher, erp.school)]
    assert "Staff meeting" in staffroom


def test_a_class_notice_reaches_only_that_class(erp):
    other_level = ClassLevel.objects.create(school=erp.school, name="Class 5", order=5)
    targeted = notice(erp, title="Class 5 excursion", audience="students")
    targeted.class_levels.add(other_level)

    assert "Class 5 excursion" not in [n.title for n in live_notices_for(erp.parent, erp.school)]
    targeted.class_levels.set([erp.level])
    assert "Class 5 excursion" in [n.title for n in live_notices_for(erp.parent, erp.school)]


def test_noticeboard_renders_for_a_family_and_hides_management(erp):
    notice(erp)
    client = Client()
    client.force_login(erp.parent)
    body = client.get("/downloads/notices/").content
    assert b"Sports day on Thursday" in body
    assert b"Publish a notice" not in body
    assert client.get("/downloads/notice/").status_code == 403


def test_publishing_a_notice_is_audited(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/downloads/notice/new/",
        {
            "title": "Half yearly exam routine",
            "body": "Published on the noticeboard.",
            "audience": "everyone",
            "publish_at": date.today().isoformat(),
            "is_pinned": "on",
        },
    )
    assert response.status_code == 302
    assert Notice.objects.get().is_pinned
    assert AuditLog.objects.filter(action="record.saved").exists()


def test_a_notice_cannot_expire_before_it_is_published(erp):
    from downloads.forms import NoticeForm

    form = NoticeForm(
        {
            "title": "Backwards",
            "body": "x",
            "audience": "everyone",
            "publish_at": "2026-09-20",
            "expires_at": "2026-09-10",
        },
        school=erp.school,
    )
    assert not form.is_valid()
    assert "expires_at" in form.errors


def test_downloads_page_shows_the_latest_notices(erp):
    notice(erp)
    client = Client()
    client.force_login(erp.parent)
    assert b"Latest notices" in client.get("/downloads/").content


@pytest.mark.parametrize(
    "role,expected,forbidden",
    [
        ("Administrator", [b"Management overview", b"Payroll register", b"Teacher load"], []),
        ("Accountant", [b"Payroll register", b"Trial balance"], [b"Student attendance"]),
        ("Teacher", [b"Student attendance", b"Teacher load"], [b"Payroll register", b"Trial balance"]),
    ],
)
def test_reports_hub_offers_only_what_a_role_may_open(erp, role, expected, forbidden):
    user = {"Administrator": erp.admin, "Accountant": erp.accountant, "Teacher": erp.teacher}[role]
    client = Client()
    client.force_login(user)
    body = client.get("/reports/").content
    for fragment in expected:
        assert fragment in body, (role, fragment)
    for fragment in forbidden:
        assert fragment not in body, (role, fragment)


def test_every_hub_link_actually_opens(erp):
    """A report list that offers a dead link is worse than a shorter list."""
    client = Client()
    client.force_login(erp.admin)
    response = client.get("/reports/")
    for item in response.context["links"]:
        assert client.get(item["url"]).status_code == 200, item["url"]


def test_student_strength_counts_by_class_and_gender(admin_client, erp):
    Student.objects.create(
        school=erp.school,
        student_id="ST2",
        first_name="Boy",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    body = admin_client.get("/reports/strength/").content
    assert b"By class and section" in body
    assert b"Class 1" in body
    assert b"By religion" in body
    assert "Girls" in admin_client.get("/reports/strength/?format=csv").content.decode("utf-8-sig")


def test_payroll_register_totals_the_month(erp):
    from finance.models import Payroll

    Payroll.objects.create(
        school=erp.school,
        employee=erp.employee,
        month=date.today().replace(day=1),
        basic=20000,
        allowance=2000,
        deduction=500,
    )
    client = Client()
    client.force_login(erp.accountant)
    body = client.get("/reports/payroll/").content
    assert b"Total net pay" in body
    assert b"Still unpaid" in body
    assert b"Teacher" in body


def test_leave_register_shows_only_your_own_to_a_teacher(erp):
    from attendance.models import LeaveRequest, LeaveType
    from employees.models import Employee

    leave_type = LeaveType.objects.create(school=erp.school, name="Casual", days_per_year=10)
    colleague = Employee.objects.create(
        school=erp.school,
        employee_id="S50",
        first_name="Colleague",
        gender="M",
        phone="01712345684",
        joining_date=date(2026, 1, 1),
    )
    for employee in (erp.employee, colleague):
        LeaveRequest.objects.create(
            school=erp.school,
            employee=employee,
            leave_type=leave_type,
            start_date=date.today(),
            end_date=date.today(),
            status="approved",
        )
    client = Client()
    client.force_login(erp.teacher)
    body = client.get(f"/reports/leave/?start={date.today()}&end={date.today()}").content
    assert b"Colleague" not in body

    client.force_login(erp.admin)
    assert b"Colleague" in client.get(f"/reports/leave/?start={date.today()}&end={date.today()}").content


def test_teacher_load_lists_periods_per_week(erp):
    from datetime import time

    from timetable.models import Period, RoutineSlot

    period = Period.objects.create(
        school=erp.school, name="Period 1", order=1, start_time=time(9), end_time=time(9, 45)
    )
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=1,
        period=period,
        subject=erp.subject,
        teacher=erp.employee,
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/reports/teacher-load/").content
    assert b"Periods per week" in body
    assert b"Periods assigned" in body


@pytest.mark.parametrize("url", ["/reports/overview/", "/reports/strength/", "/reports/teacher-load/"])
@pytest.mark.parametrize("fmt", ["csv", "xlsx", "pdf"])
def test_hub_reports_export_in_every_format(admin_client, erp, url, fmt):
    response = admin_client.get(f"{url}?format={fmt}")
    assert response.status_code == 200 and response.content


def test_overview_reports_working_days_and_dues(erp, invoice):
    client = Client()
    client.force_login(erp.accountant)
    body = client.get("/reports/overview/?start=2026-09-01&end=2026-09-30").content
    assert b"Working days in range" in body
    assert b"Outstanding amount now" in body


def test_families_get_their_own_records_in_the_hub(erp):
    client = Client()
    client.force_login(erp.parent)
    body = client.get("/reports/").content
    assert b"Your records" in body
    assert b"My results" in body
    assert b"Payroll register" not in body


def test_an_unprivileged_account_sees_an_empty_school_section(erp):
    user = User.objects.create_user("plain", school=erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name="Staff"))
    client = Client()
    client.force_login(user)
    body = client.get("/reports/").content
    assert b"Payroll register" not in body
    assert b"Trial balance" not in body
