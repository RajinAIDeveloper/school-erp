"""
Phase 4c: an accepted place becomes a student without anything typed twice; the funnel report;
and applicants' data kept no longer than the school says.
"""

from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone

from academics.models import AcademicYear
from admissions import privacy, services
from admissions.enrol import enrol
from admissions.models import Application, ApplicationDocument
from messaging.models import SMSMessage
from students.models import Enrollment, GuardianConsent, Student, StudentDocument
from tests.admissions_support import apply_online, family, login

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def photo_upload():
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (300, 400), "#94a3b8").save(out, "JPEG")
    return SimpleUploadedFile("face.jpg", out.getvalue(), content_type="image/jpeg")


def accepted(erp, admissions, **changes):
    application, raw = apply_online(admissions.row, **changes)
    services.transition(application, "offered", user=erp.admin)
    services.transition(application, "accepted")
    application.refresh_from_db()
    return application, raw


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def test_an_accepted_child_becomes_a_student_with_everything_carried_over(erp, admissions, media):
    application, _ = accepted(erp, admissions, name_bn="রাফি", birth_registration_no="20202692500000001")
    services.add_document(
        application=application,
        kind="birth_certificate",
        upload=SimpleUploadedFile("b.pdf", PDF, content_type="application/pdf"),
    )
    services.add_document(application=application, kind="photo", upload=photo_upload())
    rejected = services.add_document(
        application=application,
        kind="report_card",
        upload=SimpleUploadedFile("r.pdf", PDF, content_type="application/pdf"),
    )
    services.check_document(user=erp.staff, document=rejected, accept=False, reason="Wrong child")
    student = enrol(user=erp.admin, application=application, section=erp.section, future_ok=True)
    assert student.student_id == "2027-0001" and student.full_name == "Rafi Ahmed"
    assert student.name_bn == "রাফি" and student.birth_registration_no == "20202692500000001"
    assert student.date_of_birth == application.date_of_birth and student.present_address.startswith("House 1")
    enrollment = Enrollment.objects.get(student=student)
    assert enrollment.academic_year == admissions.year and enrollment.section == erp.section
    guardian = student.primary_guardian
    assert guardian.full_name == "Nasrin Ahmed" and guardian.phone == "01711000001"
    # Copies, so clearing the application later leaves the student's own.
    assert student.photo and StudentDocument.objects.filter(student=student).count() == 2
    copy = StudentDocument.objects.filter(student=student).first()
    assert copy.file.name != application.documents.first().file.name
    consent = GuardianConsent.objects.get(student=student)
    assert consent.method == "application" and consent.given and "2026-09" in consent.note
    application.refresh_from_db()
    assert application.status == "enrolled" and application.student == student and application.enrolled_at


def test_a_future_year_needs_confirming(erp, admissions, media):
    application, _ = accepted(erp, admissions)
    with pytest.raises(ValidationError, match="not the current year"):
        enrol(user=erp.admin, application=application, section=erp.section)
    assert not Student.objects.filter(first_name="Rafi").exists()


def test_a_guardian_with_the_same_number_is_attached_only_once_confirmed(erp, admissions, media):
    application, _ = accepted(erp, admissions, guardian_phone=erp.guardian.phone)
    with pytest.raises(ValidationError, match="same family"):
        enrol(user=erp.admin, application=application, section=erp.section, future_ok=True)
    student = enrol(user=erp.admin, application=application, section=erp.section, future_ok=True, same_family=True)
    assert student.primary_guardian == erp.guardian
    assert set(erp.guardian.student_links.values_list("student__first_name", flat=True)) == {"Ayesha", "Rafi"}


def test_only_an_accepted_place_in_the_right_class_is_enrolled_by_a_manager(erp, admissions, media):
    from academics.models import ClassLevel, Section

    waiting, _ = apply_online(admissions.row)
    with pytest.raises(ValidationError, match="not accepted"):
        enrol(user=erp.admin, application=waiting, section=erp.section, future_ok=True)
    application, _ = accepted(erp, admissions, first_name="Tanvir")
    with pytest.raises(PermissionDenied):
        enrol(user=erp.staff, application=application, section=erp.section, future_ok=True)
    other = Section.objects.create(
        school=erp.school, class_level=ClassLevel.objects.create(school=erp.school, name="Class 2", order=2), name="A"
    )
    with pytest.raises(ValidationError, match="not a section of"):
        enrol(user=erp.admin, application=application, section=other, future_ok=True)


def test_the_welcome_message_goes_only_where_the_school_allows_it(
    erp, admissions, media, django_capture_on_commit_callbacks
):
    application, _ = accepted(erp, admissions)
    with django_capture_on_commit_callbacks(execute=True):
        enrol(user=erp.admin, application=application, section=erp.section, future_ok=True)
    assert not SMSMessage.objects.exists()
    erp.school.notify_admission_sms = True
    erp.school.save()
    second, _ = accepted(erp, admissions, first_name="Tanvir", guardian_phone="01711000077")
    with django_capture_on_commit_callbacks(execute=True):
        enrol(user=erp.admin, application=second, section=erp.section, future_ok=True)
    assert SMSMessage.objects.filter(phone__endswith="1711000077").count() == 1


def test_the_enrol_screen_enrols_the_ticked_rows(erp, admissions, media):
    first, _ = accepted(erp, admissions)
    second, _ = accepted(erp, admissions, first_name="Tanvir", guardian_phone=erp.guardian.phone)
    client = login(erp.admin)
    url = f"/admissions/classes/{admissions.row.pk}/enrol/"
    page = client.get(url).content.decode()
    assert "is not the current year" in page and "Same family" in page
    response = client.post(
        url,
        {
            "application": [first.pk, second.pk],
            f"section_{first.pk}": erp.section.pk,
            f"section_{second.pk}": erp.other_section.pk,
            "future_ok": "on",
        },
        follow=True,
    )
    body = response.content.decode()
    assert "same family" in body  # the second needs the family confirmed
    assert Application.objects.get(pk=first.pk).status == "enrolled"
    assert Application.objects.get(pk=second.pk).status == "accepted"
    assert login(erp.staff).get(url).status_code == 403


# ------------------------------------------------------------------ the report


def test_the_funnel_counts_each_stage_and_survives_clearing(erp, admissions, media):
    first, _ = accepted(erp, admissions, heard_from="website")
    enrol(user=erp.admin, application=first, section=erp.section, future_ok=True)
    offered, _ = apply_online(admissions.row, first_name="Tanvir", heard_from="website")
    services.transition(offered, "offered", user=erp.admin)
    turned, _ = apply_online(admissions.row, first_name="Mitu", heard_from="family")
    services.transition(turned, "not_offered", user=erp.admin, reason="Full")
    before = login(erp.admin).get(f"/admissions/rounds/{admissions.round.pk}/report/").content.decode()
    privacy.clear_application(Application.objects.get(pk=turned.pk))
    from admissions.report import funnel

    [row], sources = funnel(admissions.round)
    assert (row["applied"], row["offered"], row["accepted"], row["enrolled"]) == (3, 2, 1, 1)
    assert row["filled"] == 50 and dict(sources) == {"The school's website": 2, "Family or friends": 1}
    assert "The school&#x27;s website" in before or "The school's website" in before
    export = login(erp.admin).get(f"/admissions/rounds/{admissions.round.pk}/report/?format=csv")
    assert export["Content-Type"].startswith("text/csv") and b"Applied online" in export.content


# ------------------------------------------------------------------ keeping data no longer than needed


def test_unsuccessful_applications_are_cleared_after_the_keeping_period(erp, admissions, media):
    kept, _ = accepted(erp, admissions)
    turned, raw = apply_online(admissions.row, first_name="Mitu")
    services.add_document(
        application=turned,
        kind="birth_certificate",
        upload=SimpleUploadedFile("b.pdf", PDF, content_type="application/pdf"),
    )
    services.transition(turned, "not_offered", user=erp.admin, reason="Full")
    stored = ApplicationDocument.objects.get(application=turned)
    today = admissions.round.closes_on + timedelta(days=200)  # six months and more after closing
    assert privacy.purge(erp.school, admissions.round.closes_on + timedelta(days=100)) == 0
    assert privacy.purge(erp.school, today, dry_run=True) == 2
    assert privacy.purge(erp.school, today) == 2
    turned.refresh_from_db()
    assert turned.purged_at and turned.first_name == "Cleared" and turned.guardian_phone == ""
    assert turned.status == "not_offered" and turned.date_of_birth.month == 1
    assert not turned.events.exists() and not ApplicationDocument.objects.filter(pk=stored.pk).exists()
    assert not stored.file.storage.exists(stored.file.name)
    assert link_status(erp, raw) == 404
    # An accepted place never enrolled is cleared too; nothing enrolled is touched yet.
    kept.refresh_from_db()
    assert kept.purged_at is not None


def link_status(erp, raw):
    from django.test import Client

    return Client().get(f"/apply/{erp.school.slug}/t/{raw}/").status_code


def test_an_enrolled_childs_application_is_cleared_90_days_after_enrolment(erp, admissions, media):
    application, _ = accepted(erp, admissions)
    student = enrol(user=erp.admin, application=application, section=erp.section, future_ok=True)
    today = timezone.localdate()
    assert privacy.purge(erp.school, today) == 0
    Application.objects.filter(pk=application.pk).update(enrolled_at=timezone.now() - timedelta(days=91))
    call_command("admissions_purge", verbosity=0)
    application.refresh_from_db()
    assert application.purged_at and application.student == student
    student.refresh_from_db()
    assert student.first_name == "Rafi" and StudentDocument.objects.filter(student=student).count() == 0


def test_erasing_a_student_clears_the_application_they_came_through(erp, admissions, media):
    from students.privacy import erase_student

    application, _ = accepted(erp, admissions)
    student = enrol(user=erp.admin, application=application, section=erp.section, future_ok=True)
    Student.objects.filter(pk=student.pk).update(status="withdrawn")
    Enrollment.objects.filter(student=student).update(status="left")
    # Long gone: the year they were enrolled for ended before the retention period.
    AcademicYear.objects.filter(pk=admissions.year.pk).update(start_date=date(2020, 1, 1), end_date=date(2020, 12, 31))
    erp.school.retention_years = 0
    erp.school.save()
    erase_student(school=erp.school, user=erp.admin, student=student, confirm=student.student_id)
    application.refresh_from_db()
    assert application.purged_at and application.guardian_name == "" and application.first_name == "Cleared"


def test_the_family_page_answers_nothing_once_cleared(erp, admissions, media):
    application, raw = apply_online(admissions.row)
    client = family(erp.school, raw)
    privacy.clear_application(application)
    assert "Rafi" not in client.get(f"/apply/{erp.school.slug}/my/").content.decode()


def test_the_demo_school_shows_admissions_working(settings, tmp_path, db):
    settings.MEDIA_ROOT = tmp_path
    call_command("seed_demo", slug="seed-test", demo_password="Seed-pass-2026", verbosity=0)
    statuses = dict((a.first_name, a.status) for a in Application.objects.filter(school__slug="seed-test"))
    assert statuses == {
        "Nusrat": "accepted",
        "Rafi": "offered",
        "Tahmid": "waitlisted",
        "Mehjabin": "not_offered",
        "Samiul": "under_review",
        "Ariana": "submitted",
    }
    call_command("seed_demo", slug="seed-test", demo_password="Seed-pass-2026", verbosity=0)
    assert Application.objects.filter(school__slug="seed-test").count() == 6
