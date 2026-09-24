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
    assert Mark.objects.filter(school=school, schedule__exam__name="First Term").count() == 5
    assert LeaveRequest.objects.filter(school=school, status="pending").count() == 1
    assert DownloadItem.objects.filter(school=school).count() == 1

    marks = Mark.objects.filter(school=school).count()
    call_command("seed_demo", slug="seed-test", demo_password="Changed-pass", verbosity=0)
    assert User.objects.get(username="demo_admin").check_password("Seed-pass-2026")
    assert FeeInvoice.objects.filter(school=school).count() == 5
    assert Mark.objects.filter(school=school).count() == marks


@pytest.mark.django_db
def test_seed_demo_shows_each_programme_with_published_results(settings, tmp_path):
    """Cambridge, IB and national classes, each worked out by its own rules and published."""
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo", slug="seed-test", demo_password="Seed-pass-2026", verbosity=0)

    from examinations.models import Exam, OfficialResult, ResultSnapshot, SeriesEntry
    from students.models import Guardian

    exams = {e.name: e for e in Exam.objects.filter(school__slug="seed-test", status="published")}
    assert set(exams) == {"IGCSE Mock", "DP Mock", "Half Yearly (Class 9)"}

    igcse = ResultSnapshot.objects.filter(exam=exams["IGCSE Mock"])
    assert igcse.count() == 4
    zara = igcse.get(payload__student_code="IG-001").payload
    assert zara["rulebook"].startswith("Cambridge") and zara["gpa"] is None
    assert zara["overall_comment"] and zara["forecasts"][0]["grade"] == "A*"
    physics = next(c for c in zara["cells"] if c["subject_code"] == "0625")
    assert [p["code"] for p in physics["components"]] == ["p2", "p4", "practical"]
    # Nusrat took Chemistry and Business, not Physics.
    nusrat = igcse.get(payload__student_code="IG-003").payload
    assert "0625" not in {c["subject_code"] for c in nusrat["cells"]}

    samira = ResultSnapshot.objects.get(exam=exams["DP Mock"], payload__student_code="DP-001").payload
    assert "diploma conditions met" in samira["headline"] and "school estimate" in samira["headline"]
    arif = ResultSnapshot.objects.get(exam=exams["DP Mock"], payload__student_code="DP-002").payload
    assert "conditions not met" in arif["headline"]

    nabila = ResultSnapshot.objects.get(exam=exams["Half Yearly (Class 9)"], payload__student_code="C9-001").payload
    assert nabila["gpa"] and nabila["fourth_subject"] and nabila["group"] == "Science"

    assert SeriesEntry.objects.filter(candidate__series__name="June 2027").count() == 16
    assert OfficialResult.objects.get().checked_at is not None
    family = Guardian.objects.get(user__username="demo_guardian")
    assert family.student_links.count() == 2
