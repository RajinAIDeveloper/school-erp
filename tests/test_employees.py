"""Staff roster, the personal file, documents and salary visibility."""

from datetime import date

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from attendance.models import LeaveRequest, LeaveType, StaffAttendance
from attendance.services import leave_balance
from employees.models import Department, Designation, Employee, EmployeeDocument
from users.models import User


def make_staff(erp, **overrides):
    values = dict(
        school=erp.school,
        employee_id="S9",
        employee_type="staff",
        first_name="Nadia",
        gender="F",
        phone="01712345612",
        joining_date=date(2026, 1, 1),
        basic_salary=18000,
    )
    values.update(overrides)
    return Employee.objects.create(**values)


def test_employee_list_filters_and_search(admin_client, erp):
    department = Department.objects.create(school=erp.school, name="Science")
    make_staff(erp, department=department, first_name="Nadia")
    assert b"Nadia" in admin_client.get("/employees/").content
    assert b"Nadia" in admin_client.get(f"/employees/?department={department.pk}").content
    assert b"Nadia" not in admin_client.get("/employees/?employee_type=teacher").content
    assert b"Nadia" in admin_client.get("/employees/?q=Nadia").content


def test_salary_is_hidden_from_roles_without_payroll_access(erp):
    make_staff(erp)
    client = Client()
    client.force_login(erp.accountant)
    assert b"Basic salary" in client.get("/employees/").content

    principal = User.objects.create_user("head", school=erp.school, password="Test-pass-9842")
    from django.contrib.auth.models import Group

    principal.groups.add(Group.objects.get(name="Principal"))
    client.force_login(principal)
    body = client.get("/employees/").content
    assert b"Basic salary" not in body
    assert b"Nadia" in body


def test_employee_form_drops_salary_without_payroll_rights(erp):
    from employees.forms import EmployeeForm

    assert "basic_salary" in EmployeeForm(school=erp.school, can_see_salary=True).fields
    assert "basic_salary" not in EmployeeForm(school=erp.school, can_see_salary=False).fields


def test_employee_detail_shows_assignments_attendance_and_leave(admin_client, erp):
    LeaveType.objects.create(school=erp.school, name="Casual", days_per_year=10)
    StaffAttendance.objects.create(school=erp.school, employee=erp.employee, date=date.today(), status="present")
    body = admin_client.get(f"/employees/{erp.employee.pk}/").content
    assert b"Teaching assignments" in body
    assert b"Math" in body
    assert b"Leave balance" in body
    assert b"Casual" in body


def test_employee_detail_hides_salary_and_payroll_from_the_principal(erp):
    from django.contrib.auth.models import Group

    principal = User.objects.create_user("head2", school=erp.school, password="Test-pass-9842")
    principal.groups.add(Group.objects.get(name="Principal"))
    client = Client()
    client.force_login(principal)
    body = client.get(f"/employees/{erp.employee.pk}/").content
    assert b"Payroll history" not in body
    client.force_login(erp.accountant)
    assert b"Payroll history" in client.get(f"/employees/{erp.employee.pk}/").content


def test_employee_detail_is_school_scoped(admin_client, erp):
    foreign = Employee.objects.create(
        school=erp.other,
        employee_id="F1",
        first_name="Outsider",
        gender="M",
        phone="01712345613",
        joining_date=date(2026, 1, 1),
    )
    assert admin_client.get(f"/employees/{foreign.pk}/").status_code == 404


def test_a_teacher_has_no_staff_roster_but_keeps_their_own_file(erp):
    """A colleague's phone, NID and qualification are the office's business, not a teacher's."""
    colleague = make_staff(erp, employee_id="S21", first_name="Colleague", phone="01712345615")
    client = Client()
    client.force_login(erp.teacher)
    assert client.get("/employees/").status_code == 403
    assert client.get("/employees/export/").status_code == 403
    assert client.get(f"/employees/{colleague.pk}/").status_code == 403
    # Their own file is still one click away.
    assert client.get("/employees/me/").status_code == 302
    assert client.get(f"/employees/{erp.employee.pk}/").status_code == 200


def test_staff_may_open_their_own_file_but_not_the_roster(erp):
    """A staff member is not an HR browser; they may still read their own record."""
    mine = make_staff(erp, user=erp.staff, employee_id="S11", first_name="Mine")
    other = make_staff(erp, employee_id="S12", first_name="Colleague", phone="01712345614")
    client = Client()
    client.force_login(erp.staff)
    assert client.get("/employees/").status_code == 403
    assert client.get(f"/employees/{mine.pk}/").status_code == 200
    assert client.get(f"/employees/{other.pk}/").status_code == 403


def test_my_staff_file_redirects_or_explains(erp):
    client = Client()
    client.force_login(erp.teacher)
    response = client.get("/employees/me/")
    assert response.status_code == 302 and response.url == f"/employees/{erp.employee.pk}/"

    unlinked = User.objects.create_user("lonely", school=erp.school, password="Test-pass-9842")
    from django.contrib.auth.models import Group

    unlinked.groups.add(Group.objects.get(name="Staff"))
    client.force_login(unlinked)
    assert client.get("/employees/me/").status_code == 404


def test_employee_documents_are_private_to_managers_and_their_owner(erp, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    mine = make_staff(erp, user=erp.staff, employee_id="S10", first_name="Own")
    document = EmployeeDocument.objects.create(
        school=erp.school,
        employee=mine,
        title="Appointment letter",
        file=SimpleUploadedFile("letter.txt", b"private terms"),
    )
    client = Client()
    client.force_login(erp.admin)
    response = client.get(f"/employees/documents/{document.pk}/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"private terms"

    client.force_login(erp.staff)
    assert client.get(f"/employees/documents/{document.pk}/").status_code == 200

    other = User.objects.create_user("nosy", school=erp.school, password="Test-pass-9842")
    from django.contrib.auth.models import Group

    other.groups.add(Group.objects.get(name="Staff"))
    client.force_login(other)
    assert client.get(f"/employees/documents/{document.pk}/").status_code == 404

    # A guardian gets 404 rather than 403: the answer must not confirm the file exists.
    client.force_login(erp.parent)
    assert client.get(f"/employees/documents/{document.pk}/").status_code == 404


def test_employee_export_includes_salary_only_for_payroll_roles(erp):
    """The accountant runs payroll, so they export the roster; a teacher has no roster at all."""
    make_staff(erp, designation=Designation.objects.create(school=erp.school, name="Clerk"))
    client = Client()
    client.force_login(erp.accountant)
    assert "Basic salary" in client.get("/employees/export/").content.decode("utf-8-sig")

    principal = User.objects.create_user("exporter", school=erp.school, password="Test-pass-9842")
    from django.contrib.auth.models import Group

    principal.groups.add(Group.objects.get(name="Principal"))
    client.force_login(principal)
    body = client.get("/employees/export/").content.decode("utf-8-sig")
    assert "Basic salary" not in body and "Nadia" in body

    client.force_login(erp.teacher)
    assert client.get("/employees/export/").status_code == 403


def test_leave_balance_counts_only_approved_days(erp):
    leave_type = LeaveType.objects.create(school=erp.school, name="Casual", days_per_year=10)
    LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=leave_type,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 4),
        status="approved",
    )
    LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=leave_type,
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 2),
        status="pending",
    )
    row = next(r for r in leave_balance(erp.employee, 2026) if r["leave_type"] == leave_type)
    assert row["entitlement"] == 10 and row["used"] == 3 and row["remaining"] == 7
