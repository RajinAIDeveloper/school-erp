import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_seed_demo_creates_all_role_accounts_and_test_records(settings, tmp_path):
    """The documented seed command must be usable on a fresh database and on rerun."""
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo", slug="seed-test", demo_password="Seed-pass-2026", verbosity=0)

    from attendance.models import LeaveRequest, StudentAttendance
    from downloads.models import DownloadItem
    from employees.models import Employee
    from examinations.models import Mark
    from fees.models import FeeInvoice
    from students.models import Guardian, Student
    from users.models import User

    users = User.objects.filter(username__startswith="demo_")
    assert users.count() == 7
    assert {name for user in users for name in user.groups.values_list("name", flat=True)} == {
        "Administrator",
        "Principal",
        "Accountant",
        "Teacher",
        "Staff",
        "Student",
        "Guardian",
    }
    assert all(user.check_password("Seed-pass-2026") for user in users)
    school = users.get(username="demo_admin").school
    assert Student.objects.filter(school=school, user__username="demo_student").exists()
    assert Guardian.objects.filter(school=school, user__username="demo_guardian").exists()
    assert Employee.objects.filter(school=school, employee_id__in=["DEMO-T1", "DEMO-S1"]).count() == 2
    assert StudentAttendance.objects.filter(school=school).count() == 5
    assert FeeInvoice.objects.filter(school=school).count() == 5
    assert Mark.objects.filter(school=school).count() == 5
    assert LeaveRequest.objects.filter(school=school, status="pending").count() == 1
    assert DownloadItem.objects.filter(school=school).count() == 1

    call_command("seed_demo", slug="seed-test", demo_password="Changed-pass", verbosity=0)
    assert User.objects.get(username="demo_admin").check_password("Seed-pass-2026")
    assert FeeInvoice.objects.filter(school=school).count() == 5
    assert Mark.objects.filter(school=school).count() == 5
