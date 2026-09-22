"""Provisioning logins, forced password change and administrator resets."""

from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from core.models import AuditLog
from employees.models import Employee
from messaging.models import SMSMessage
from students.models import Enrollment, Guardian, Student, StudentGuardian
from users.models import User
from users.services import provision_login, provision_section, reset_password, unique_username


def test_provisioning_a_student_links_the_record_and_sets_the_role(erp):
    account, password = provision_login(school=erp.school, user=erp.admin, profile=erp.student)
    erp.student.refresh_from_db()
    assert erp.student.user == account
    assert account.school == erp.school
    assert account.username == "s1"
    assert [g.name for g in account.groups.all()] == ["Student"]
    assert account.must_change_password
    assert account.check_password(password)
    assert not account.is_staff and not account.is_superuser
    assert AuditLog.objects.filter(action="user.provisioned").exists()


def test_a_guardian_gets_the_guardian_role_and_a_teacher_the_teacher_role(erp):
    guardian = Guardian.objects.create(school=erp.school, full_name="New parent", phone="01712345688")
    account, _ = provision_login(school=erp.school, user=erp.admin, profile=guardian)
    assert [g.name for g in account.groups.all()] == ["Guardian"]

    staff = Employee.objects.create(
        school=erp.school,
        employee_id="S30",
        employee_type="staff",
        first_name="Office",
        gender="F",
        phone="01712345689",
        joining_date=date(2026, 1, 1),
    )
    account, _ = provision_login(school=erp.school, user=erp.admin, profile=staff)
    assert [g.name for g in account.groups.all()] == ["Staff"]


def test_provisioning_twice_is_refused(erp):
    provision_login(school=erp.school, user=erp.admin, profile=erp.student)
    with pytest.raises(ValidationError):
        provision_login(school=erp.school, user=erp.admin, profile=erp.student)
    assert User.objects.filter(student_profile=erp.student).count() == 1


def test_provisioning_refuses_a_record_from_another_school(erp):
    foreign = Student.objects.create(
        school=erp.other,
        student_id="OTHER-1",
        first_name="Elsewhere",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    with pytest.raises(PermissionDenied):
        provision_login(school=erp.school, user=erp.admin, profile=foreign)


def test_provisioning_needs_the_permission(erp):
    with pytest.raises(PermissionDenied):
        provision_login(school=erp.school, user=erp.teacher, profile=erp.student)


def test_usernames_do_not_collide(erp):
    User.objects.create_user(username="s1", school=erp.school)
    account, _ = provision_login(school=erp.school, user=erp.admin, profile=erp.student)
    assert account.username == "s1-2"
    assert unique_username("S1") == "s1-3"


def test_provision_screen_shows_the_password_once(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post(f"/users/provision/student/{erp.student.pk}/", {"next": "/students/"})
    assert response.status_code == 200
    assert b"only time these passwords are shown" in response.content
    assert b"Ayesha" in response.content
    erp.student.refresh_from_db()
    assert erp.student.user is not None


def test_provisioning_a_whole_section_skips_anyone_who_has_a_login(erp):
    student = Student.objects.create(
        school=erp.school,
        student_id="B2",
        first_name="Bilal",
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
        roll_number=2,
    )
    guardian = Guardian.objects.create(school=erp.school, full_name="Bilal's parent", phone="01712345687")
    StudentGuardian.objects.create(student=student, guardian=guardian, relation="father", is_primary=True)

    rows = provision_section(school=erp.school, user=erp.admin, section=erp.section)
    # Two students plus Bilal's guardian; Ayesha's guardian already has an account.
    assert len(rows) == 3
    assert {row["role"] for row in rows} == {"Student", "Guardian"}

    assert provision_section(school=erp.school, user=erp.admin, section=erp.section) == []


def test_provisioning_a_section_can_skip_guardians(erp):
    rows = provision_section(school=erp.school, user=erp.admin, section=erp.section, include_guardians=False)
    assert [row["role"] for row in rows] == ["Student"]


def test_bulk_provision_screen_offers_a_one_time_csv(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post("/users/provision/", {"section": erp.section.pk, "include_guardians": "on"})
    assert response.status_code == 200
    assert b"Download CSV" in response.content

    csv_response = client.get("/users/provision/credentials.csv")
    assert csv_response.status_code == 200
    assert "Temporary password" in csv_response.content.decode("utf-8-sig")

    # The credentials are not kept after that download.
    assert client.get("/users/provision/credentials.csv").status_code == 404


def test_login_sms_only_when_admission_messages_are_on(erp):
    provision_login(school=erp.school, user=erp.admin, profile=erp.student, send_sms=True)
    assert not SMSMessage.objects.exists()

    erp.school.notify_admission_sms = True
    erp.school.save()
    guardian = Guardian.objects.create(school=erp.school, full_name="Texted", phone="01712345686")
    provision_login(school=erp.school, user=erp.admin, profile=guardian, send_sms=True)
    message = SMSMessage.objects.get()
    assert "temporary password" in message.body


def test_a_temporary_password_holds_the_user_on_the_password_screen(erp):
    account, password = provision_login(school=erp.school, user=erp.admin, profile=erp.student)
    client = Client()
    assert client.login(username=account.username, password=password)

    redirected = client.get("/")
    assert redirected.status_code == 302 and redirected.url == "/password/change/"
    assert client.get("/downloads/").status_code == 302
    # The password screen itself and signing out stay reachable.
    assert client.get("/password/change/").status_code == 200

    changed = client.post(
        "/password/change/",
        {"old_password": password, "new_password1": "Chosen-by-me-8812", "new_password2": "Chosen-by-me-8812"},
    )
    assert changed.status_code == 302
    account.refresh_from_db()
    assert not account.must_change_password
    assert client.get("/").status_code == 200
    assert AuditLog.objects.filter(action="user.password_changed").exists()


def test_administrator_reset_issues_a_new_temporary_password(erp):
    original = erp.teacher.password
    password = reset_password(school=erp.school, user=erp.admin, account=erp.teacher)
    erp.teacher.refresh_from_db()
    assert erp.teacher.password != original
    assert erp.teacher.check_password(password)
    assert erp.teacher.must_change_password
    assert AuditLog.objects.filter(action="user.password_reset").exists()


def test_reset_refuses_another_school_and_a_superuser(erp):
    foreign = User.objects.create_user("outsider", school=erp.other, password="Test-pass-9842")
    with pytest.raises(PermissionDenied):
        reset_password(school=erp.school, user=erp.admin, account=foreign)

    platform = User.objects.create_superuser("platform", password="Test-pass-9842")
    platform.school = erp.school
    platform.save()
    admin_group = Group.objects.get(name="Administrator")
    school_admin = User.objects.create_user("school-admin", school=erp.school, password="Test-pass-9842")
    school_admin.groups.add(admin_group)
    with pytest.raises(PermissionDenied):
        reset_password(school=erp.school, user=school_admin, account=platform)


def test_reset_screen_shows_the_password_once(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post(f"/users/{erp.teacher.pk}/reset/")
    assert response.status_code == 200
    assert b"only time these passwords are shown" in response.content


def test_user_detail_shows_the_linked_record(erp):
    provision_login(school=erp.school, user=erp.admin, profile=erp.student)
    erp.student.refresh_from_db()
    client = Client()
    client.force_login(erp.admin)
    body = client.get(f"/users/{erp.student.user.pk}/").content
    assert b"Student: Ayesha" in body
    assert b"temporary password" in body


def test_user_list_filters_by_role(admin_client, erp):
    assert b"teacher" in admin_client.get("/users/?groups__name=Teacher").content
    assert b"accountant" not in admin_client.get("/users/?groups__name=Teacher").content


def test_provisioning_from_a_screen_is_refused_without_the_record_permission(erp):
    client = Client()
    client.force_login(erp.accountant)
    response = client.post(f"/users/provision/student/{erp.student.pk}/")
    assert response.status_code == 403
