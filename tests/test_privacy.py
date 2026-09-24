"""
The privacy baseline: guardian consent per purpose, identity numbers kept from staff who do
not need them, sensitive views logged, and erasure that leaves no personal data behind while
keeping the school's accounts.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from core.models import AuditLog
from examinations.services import publish_exam, save_mark
from students.models import GuardianConsent, Student
from students.privacy import erasable, erase_student, has_consent, record_consent


def login(user):
    client = Client()
    client.force_login(user)
    return client


# ============================================================ consent


def test_the_latest_consent_record_stands_and_the_history_stays(erp):
    record_consent(user=erp.admin, student=erp.student, purpose="abroad", given=True)
    assert has_consent(erp.student, "abroad")
    record_consent(user=erp.admin, student=erp.student, purpose="abroad", given=False)
    assert not has_consent(erp.student, "abroad")
    assert GuardianConsent.objects.filter(purpose="abroad").count() == 2


def test_a_guardian_gives_and_withdraws_consent_in_the_portal(erp):
    family = login(erp.parent)
    page = family.get(f"/portal/privacy/?student={erp.student.pk}").content.decode()
    assert "Sharing entry and result data with awarding bodies outside Bangladesh" in page
    family.post(f"/portal/privacy/?student={erp.student.pk}", {"purpose": "photos", "given": "1"})
    record = GuardianConsent.objects.get()
    assert record.given and record.method == "portal" and record.recorded_by == erp.parent


def test_only_the_childs_guardian_consents_in_the_portal(erp):
    with pytest.raises(PermissionDenied):
        record_consent(user=erp.teacher, student=erp.student, purpose="photos", given=True, method="portal")
    with pytest.raises(PermissionDenied):
        record_consent(user=erp.teacher, student=erp.student, purpose="photos", given=True)


def test_entries_abroad_are_flagged_without_consent(erp):
    from examinations.candidates import entry_problems
    from examinations.models import ExamSeries
    from examinations.official import add_candidates

    series = ExamSeries.objects.create(school=erp.school, body="cambridge", name="June 2027", centre_number="BD1")
    add_candidates(user=erp.admin, series=series, students=[erp.student])
    assert any("consent to share data" in p for p in entry_problems(series))
    record_consent(user=erp.admin, student=erp.student, purpose="abroad", given=True)
    assert not any("consent to share data" in p for p in entry_problems(series))


# ============================================================ restricted data


def test_identity_numbers_are_shown_to_managers_only(erp):
    erp.student.birth_registration_no = "20161234567890123"
    erp.student.save()
    assert "20161234567890123" in login(erp.admin).get(f"/students/{erp.student.pk}/").content.decode()
    teacher_page = login(erp.teacher).get(f"/students/{erp.student.pk}/").content.decode()
    assert "20161234567890123" not in teacher_page and "managers only" in teacher_page


def test_opening_registration_data_is_logged(erp):
    login(erp.admin).get(f"/exams/registration/?class_level={erp.level.pk}")
    assert AuditLog.objects.filter(action="privacy.viewed", description__startswith="Board registration").exists()


# ============================================================ retention and erasure


@pytest.fixture
def former(erp):
    """Ayesha left years ago, with a published result, a receipt, a message and a guardian."""
    from fees.services import collect_payment, generate_invoices
    from messaging.models import SMSMessage

    s = erp.student
    s.last_name, s.father_name, s.mother_name = "Rahman", "Karim Rahman", "Salma Begum"
    s.father_nid, s.birth_registration_no, s.present_address = "1234567890", "20161234567890123", "House 5, Road 2"
    s.save()
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    from fees.models import FeeInvoice

    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=FeeInvoice.objects.get(),
        amount=1000,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    SMSMessage.objects.create(
        school=erp.school, recipient_name="Parent", phone=erp.guardian.phone, body="Ayesha Rahman was absent today."
    )
    record_consent(user=erp.admin, student=s, purpose="records", given=True)
    s.status = Student.Status.GRADUATED
    s.save()
    erp.school.retention_years = 1
    erp.school.save()
    # Her last school year ended more than a year ago.
    from academics.models import AcademicYear

    AcademicYear.objects.filter(pk=erp.year.pk).update(start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
    s.refresh_from_db()
    return erp


def test_nothing_is_erasable_before_the_retention_period(erp, former):
    former.school.retention_years = 50
    former.school.save()
    former.student.refresh_from_db()
    assert not erasable(former.student)
    with pytest.raises(ValidationError):
        erase_student(school=former.school, user=former.admin, student=former.student, confirm="S1")


def test_erasure_needs_a_manager_and_the_id_typed(erp, former):
    with pytest.raises(PermissionDenied):
        erase_student(school=former.school, user=former.teacher, student=former.student, confirm="S1")
    with pytest.raises(ValidationError):
        erase_student(school=former.school, user=former.admin, student=former.student, confirm="S2")


def test_erasure_leaves_no_personal_data_but_keeps_the_accounts(erp, former):
    from examinations.models import ResultSnapshot
    from fees.models import FeePayment
    from finance.models import JournalEntry
    from messaging.models import SMSMessage
    from students.models import Guardian

    erase_student(school=former.school, user=former.admin, student=former.student, confirm="S1")
    s = Student.objects.get(pk=former.student.pk)
    assert s.full_name == "Erased" and s.date_of_birth == date(2016, 1, 1)
    for value in (s.father_name, s.mother_name, s.father_nid, s.birth_registration_no, s.present_address):
        assert value == ""
    guardian = Guardian.objects.get(pk=former.guardian.pk)
    assert guardian.full_name == "Erased guardian" and guardian.phone == ""
    assert not GuardianConsent.objects.exists()
    payload = ResultSnapshot.objects.get().payload
    assert payload["student"] == "Erased" and payload["father_name"] == ""
    # Nothing anywhere still carries the child's or the parents' names.
    for text in (
        str(payload),
        " ".join(SMSMessage.objects.values_list("body", flat=True)),
        " ".join(JournalEntry.objects.values_list("narration", flat=True)),
    ):
        for private in ("Ayesha", "Karim", "Salma", "1234567890"):
            assert private not in text
    assert not SMSMessage.objects.filter(phone=former.guardian.phone).exists()
    # The school keeps its money records and the marks.
    assert FeePayment.objects.count() == 1 and JournalEntry.objects.exists()
    assert ResultSnapshot.objects.get().payload["total"] == "80.00"
    former.parent.refresh_from_db()
    assert not former.parent.is_active
    assert AuditLog.objects.filter(action="student.erased").exists()


def test_a_guardian_with_another_child_here_is_kept(erp, former):
    from students.models import Enrollment, Guardian, StudentGuardian

    sibling = Student.objects.create(
        school=former.school,
        student_id="S2",
        first_name="Rafi",
        gender="M",
        date_of_birth=date(2018, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=former.school,
        student=sibling,
        academic_year=former.year,
        class_level=former.level,
        section=former.section,
        roll_number=2,
    )
    StudentGuardian.objects.create(student=sibling, guardian=former.guardian, relation="mother", is_primary=True)
    erase_student(school=former.school, user=former.admin, student=former.student, confirm="S1")
    assert Guardian.objects.get(pk=former.guardian.pk).full_name == "Parent"
    assert StudentGuardian.objects.filter(student=sibling).exists()


def test_the_retention_screen_lists_and_erases(erp, former):
    client = login(former.admin)
    page = client.get("/students/retention/").content.decode()
    assert "S1" in page
    client.post("/students/retention/", {"student": former.student.pk, "confirm": "S1"})
    assert Student.objects.get(pk=former.student.pk).first_name == "Erased"
    assert login(former.teacher).get("/students/retention/").status_code == 403
