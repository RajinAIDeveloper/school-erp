"""
Transfer, leaving and character certificates: issued frozen and numbered, revoked and
reissued with a trail, verified publicly without exposing the student, in English or Bangla.
"""

from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.utils import timezone

from students.certificates import certificate_text, issue_certificate, reissue_certificate, revoke_certificate
from students.models import Certificate


def login(user):
    client = Client()
    client.force_login(user)
    return client


def issue(erp, kind="transfer", **extra):
    details = {"leaving_date": date(2026, 9, 1), "reason": "Family moved to Chattogram"} if kind != "character" else {}
    details.update(extra)
    return issue_certificate(school=erp.school, user=erp.admin, student=erp.student, kind=kind, **details)


@pytest.fixture
def family(erp):
    erp.student.father_name = "Karim Rahman"
    erp.student.mother_name = "Salma Begum"
    erp.student.last_name = "Rahman"
    erp.student.save()
    return erp


# ============================================================ issuing


def test_a_transfer_certificate_is_numbered_and_worded_from_the_record(family):
    certificate = issue(family)
    year = timezone.localdate().year
    assert certificate.serial == f"TC-{year}-0001"
    title, paragraphs, closing = certificate_text(certificate.payload)
    assert title == "Transfer Certificate"
    assert paragraphs[0].startswith("This is to certify that Ayesha Rahman, child of Karim Rahman and Salma Begum")
    assert "from 1 January 2026 to 1 September 2026" in paragraphs[0]
    assert "Class 1, section A, roll 1" in paragraphs[0]
    assert "Reason for leaving: Family moved to Chattogram." in paragraphs
    assert closing == "We wish Ayesha every success."
    assert issue(family).serial == f"TC-{year}-0002"
    assert issue(family, kind="character").serial == f"CC-{year}-0001"


def test_a_certificate_does_not_change_when_the_record_does(family):
    certificate = issue(family)
    family.student.first_name = "Renamed"
    family.student.save()
    page = login(family.admin).get(f"/students/certificates/{certificate.pk}/").content
    assert b"Ayesha Rahman" in page and b"Renamed" not in page


def test_a_character_certificate_speaks_of_a_current_student_in_the_present(family):
    title, paragraphs, _closing = certificate_text(issue(family, kind="character", conduct="excellent").payload)
    assert title == "Character Certificate"
    assert "is a regular student of this school" in paragraphs[0]
    assert "conduct and character are excellent" in paragraphs[1]


@pytest.mark.parametrize(
    "extra,message",
    [
        ({"leaving_date": None}, "Give the date the student left."),
        ({"leaving_date": date.today() + timedelta(days=3)}, "cannot be in the future"),
        ({"leaving_date": date(2025, 12, 1)}, "before the student's admission"),
        ({"conduct": "saintly"}, "conduct"),
        ({"language": "fr"}, "English or Bangla"),
    ],
)
def test_details_that_do_not_make_sense_are_refused(family, extra, message):
    with pytest.raises(ValidationError) as caught:
        issue(family, **extra)
    assert message in " ".join(caught.value.messages)
    assert not Certificate.objects.exists()


# ============================================================ Bangla


def test_bangla_is_offered_only_when_the_school_has_switched_it_on(family):
    family.school.bangla_enabled = False
    family.school.save()
    with pytest.raises(ValidationError):
        issue(family, language="bn")
    family.school.bangla_enabled = True
    family.school.save()
    family.student.name_bn = "আয়েশা রহমান"
    family.student.father_name_bn = "করিম রহমান"
    family.student.save()
    certificate = issue(family, language="bn")
    title, paragraphs, closing = certificate_text(certificate.payload)
    assert title == "ছাড়পত্র"
    assert paragraphs[0].startswith("এই মর্মে প্রত্যয়ন করা যাচ্ছে যে, আয়েশা রহমান, পিতা: করিম রহমান, মাতা: Salma Begum")
    # Dates and numbers in Bangla digits.
    assert "১ জানুয়ারি ২০২৬ থেকে ১ সেপ্টেম্বর ২০২৬ পর্যন্ত" in paragraphs[0]
    assert "রোল: ১" in paragraphs[0]
    assert closing == "আমি তার সর্বাঙ্গীণ মঙ্গল কামনা করি।"
    page = login(family.admin).get(f"/students/certificates/{certificate.pk}/").content.decode()
    assert 'lang="bn"' in page and "এটি বিদ্যালয়ের নিজস্ব সনদ" in page


# ============================================================ fees


def test_a_transfer_certificate_waits_for_unpaid_fees(family, invoice):
    with pytest.raises(ValidationError) as caught:
        issue(family)
    assert "still owes" in " ".join(caught.value.messages)
    # A character certificate is not held back by fees.
    issue(family, kind="character")
    # Someone who manages fees may release it.
    assert issue(family, force=True).kind == "transfer"


# ============================================================ revoking and reissuing


def test_revoking_needs_a_reason_and_shows_on_the_paper_and_the_check(family):
    certificate = issue(family)
    with pytest.raises(ValidationError):
        revoke_certificate(school=family.school, user=family.admin, certificate=certificate, reason=" ")
    revoke_certificate(school=family.school, user=family.admin, certificate=certificate, reason="Issued in error")
    with pytest.raises(ValidationError):
        revoke_certificate(school=family.school, user=family.admin, certificate=certificate, reason="Again")
    page = login(family.admin).get(f"/students/certificates/{certificate.pk}/").content
    assert b"not valid" in page
    check = Client().get(f"/students/certificates/verify/{certificate.verification_code}/").content
    assert b"has been revoked and is not valid" in check


def test_reissuing_takes_the_corrected_record_and_points_the_old_one_to_the_new(family):
    old = issue(family)
    family.student.father_name = "Abdul Karim Rahman"
    family.student.save()
    new = reissue_certificate(school=family.school, user=family.admin, certificate=old)
    old.refresh_from_db()
    assert old.is_revoked and old.revoke_reason == f"Replaced by {new.serial}"
    assert new.replaces == old
    assert "Abdul Karim Rahman" in certificate_text(new.payload)[1][0]
    # The same dates and reason are carried over.
    assert new.payload["leaving_date"] == "2026-09-01"
    check = Client().get(f"/students/certificates/verify/{old.verification_code}/").content.decode()
    assert f"replaced by certificate {new.serial}" in check
    with pytest.raises(ValidationError):
        reissue_certificate(school=family.school, user=family.admin, certificate=old)


# ============================================================ verification and access


def test_the_public_check_shows_initials_and_never_the_record(family):
    certificate = issue(family)
    page = Client().get(f"/students/certificates/verify/{certificate.verification_code}/").content.decode()
    assert "is valid" in page
    assert certificate.serial in page and certificate.fingerprint in page
    assert "A•••••" in page
    for private in ("Ayesha", "Karim", "Salma", "Chattogram", "2016"):
        assert private not in page
    assert "does not confirm any" in page


def test_only_office_managers_see_and_issue_certificates(family):
    with pytest.raises(PermissionDenied):
        issue_certificate(school=family.school, user=family.teacher, student=family.student, kind="character")
    certificate = issue(family, kind="character")
    teacher = login(family.teacher)
    assert teacher.get(f"/students/{family.student.pk}/certificates/").status_code == 403
    assert teacher.get(f"/students/certificates/{certificate.pk}/").status_code == 403
    assert login(family.parent).get(f"/students/certificates/{certificate.pk}/").status_code == 403


def test_another_schools_certificate_is_not_found(family):
    from users.models import User

    certificate = issue(family, kind="character")
    outsider = User.objects.create_user(username="outsider", school=family.other, password="Test-pass-9842")
    outsider.groups.add(*family.admin.groups.all())
    assert login(outsider).get(f"/students/certificates/{certificate.pk}/").status_code == 404


# ============================================================ the screen


def test_the_certificates_screen_issues_and_keeps_typed_values_on_error(family):
    client = login(family.admin)
    url = f"/students/{family.student.pk}/certificates/"
    assert client.get(url).status_code == 200
    bad = client.post(url, {"action": "issue", "kind": "transfer", "language": "en", "reason": "Moving abroad"})
    assert bad.status_code == 200
    assert b"Give the date the student left." in bad.content
    assert b'value="Moving abroad"' in bad.content
    good = client.post(
        url,
        {"action": "issue", "kind": "transfer", "language": "en", "leaving_date": "2026-09-01", "conduct": "good"},
    )
    certificate = Certificate.objects.get()
    assert good.status_code == 302 and good["Location"].endswith(f"/students/certificates/{certificate.pk}/")
    page = client.get(good["Location"]).content
    assert b"Transfer Certificate" in page and certificate.serial.encode() in page
    assert b"not a certificate from any examination board" in page
    revoke = client.post(url, {"action": "revoke", "certificate": certificate.pk, "reason": "Wrong date"})
    assert revoke.status_code == 302
    assert Certificate.objects.get().is_revoked


# ============================================================ studentship


def test_a_studentship_certificate_says_the_child_is_studying_here_now(family):
    certificate = issue_certificate(
        school=family.school, user=family.admin, student=family.student, kind="study", reason="Passport application"
    )
    assert certificate.serial.startswith(f"SC-{timezone.localdate().year}-")
    title, paragraphs, _closing = certificate_text(certificate.payload)
    text = " ".join(paragraphs)
    assert title == "Studentship Certificate"
    assert "is a regular student of this school, now studying in Class 1" in text
    assert "Issued for: Passport application." in text
    # Printable and verifiable like every other certificate.
    assert login(family.admin).get(f"/students/certificates/{certificate.pk}/").status_code == 200
    verify = Client().get(f"/students/certificates/verify/{certificate.verification_code}/").content.decode()
    assert certificate.serial in verify


def test_a_studentship_certificate_reads_in_bangla(family):
    family.school.bangla_enabled = True
    family.school.save()
    family.student.name_bn = "আয়েশা রহমান"
    family.student.save()
    certificate = issue_certificate(
        school=family.school, user=family.admin, student=family.student, kind="study", language="bn"
    )
    title, paragraphs, _closing = certificate_text(certificate.payload)
    assert title == "অধ্যয়ন প্রত্যয়নপত্র"
    assert "বর্তমানে এই বিদ্যালয়ের" in paragraphs[0] and "আয়েশা রহমান" in paragraphs[0]


def test_a_studentship_certificate_is_only_for_a_current_student(family):
    family.student.status = family.student.Status.TRANSFERRED
    family.student.save()
    with pytest.raises(ValidationError, match="studying here now"):
        issue_certificate(school=family.school, user=family.admin, student=family.student, kind="study")


def test_a_studentship_certificate_does_not_wait_for_fees(family, invoice):
    assert invoice.balance > 0
    certificate = issue_certificate(school=family.school, user=family.admin, student=family.student, kind="study")
    assert certificate.kind == Certificate.Kind.STUDY
