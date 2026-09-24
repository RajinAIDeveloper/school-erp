"""
Phase 4a: transcripts.

A transcript shows each year's grades as awarded, with the grading key, confirmed official
results and approved predicted grades, and nothing from the frozen results that has no place
on it. It is numbered and frozen. A correction to the student's own result is flagged, to the
school and to anyone checking it; a classmate's correction is not. The public check shows the
grades to compare and the student only as initials.
"""

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from core.models import AuditLog
from examinations.models import GradeForecast, Transcript, TranscriptRequest, UnlockRequest
from examinations.services import publish_exam, review_unlock, save_marks
from examinations.transcripts import build, issue_transcript, reissue_transcript, revoke_transcript, staleness
from students.models import Enrollment, Student


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def published(erp):
    """Ayesha and a classmate, marked and published in Term 1."""
    erp.student.father_name = "Karim Rahman"
    erp.student.save()
    other = Student.objects.create(
        school=erp.school,
        student_id="S2",
        first_name="Tanvir",
        gender="M",
        date_of_birth=date(2016, 2, 2),
        admission_date=date(2026, 1, 1),
    )
    classmate = Enrollment.objects.create(
        school=erp.school,
        student=other,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    save_marks(
        user=erp.teacher,
        schedule=erp.schedule,
        section=erp.section,
        rows=[(erp.enrollment, Decimal(82), False, 0), (classmate, Decimal(55), False, 0)],
    )
    publish_exam(erp.exam, erp.admin)
    erp.exam.refresh_from_db()
    erp.classmate = classmate
    return erp


def correct(erp, enrollment, score):
    unlock = UnlockRequest.objects.create(
        school=erp.school, schedule=erp.schedule, requested_by=erp.teacher, reason="Re-totalled"
    )
    review_unlock(unlock, erp.admin, True)
    from examinations.models import Mark

    version = Mark.objects.get(schedule=erp.schedule, enrollment=enrollment).version
    save_marks(
        user=erp.teacher,
        schedule=erp.schedule,
        section=erp.section,
        rows=[(enrollment, Decimal(score), False, version)],
    )
    erp.exam.refresh_from_db()


def official(erp, *, grade="A", confirmed=True, released=True, code="0580"):
    from examinations.models import ExamSeries, OfficialResult, SeriesCandidate

    series, _ = ExamSeries.objects.get_or_create(
        school=erp.school,
        body="cambridge",
        name="June 2026",
        defaults={"results_date": timezone.localdate() - timedelta(days=1)},
    )
    if not released:
        series.results_date = timezone.localdate() + timedelta(days=5)
        series.save()
    candidate, _ = SeriesCandidate.objects.get_or_create(
        school=erp.school, series=series, student=erp.student, defaults={"candidate_number": "0001"}
    )
    return OfficialResult.objects.create(
        school=erp.school,
        candidate=candidate,
        syllabus_code=code,
        syllabus_title="Mathematics",
        grade=grade,
        source="Statement of results",
        received_on=timezone.localdate(),
        checked_by=erp.admin if confirmed else None,
        checked_at=timezone.now() if confirmed else None,
    )


# ------------------------------------------------------------------ what goes on it


def test_a_transcript_shows_the_grades_as_awarded_and_nothing_private(published):
    erp = published
    payload, sources = build(erp.student)
    (year,) = payload["years"]
    assert year["year"] == "2026" and year["basis"] == "Term 1"
    (subject,) = year["subjects"]
    assert subject["name"] == "Math" and subject["letter"] and subject["percent"] is not None
    assert payload["keys"] and payload["keys"][0]["rules"]
    assert sources["snapshots"] == [["exam", sources["snapshots"][0][1], erp.exam.publication_version]]
    # Parents, comments and ranks from the frozen result are never copied.
    text = json.dumps(payload)
    assert "Karim Rahman" not in text and "rank" not in text and "comments" not in text
    without = build(erp.student, with_percent=False)[0]
    assert without["years"][0]["subjects"][0]["percent"] is None


def test_only_confirmed_released_official_results_and_approved_predictions(published):
    erp = published
    official(erp, grade="A", code="0580")
    official(erp, grade="B", code="0620", confirmed=False)
    GradeForecast.objects.create(
        school=erp.school,
        enrollment=erp.enrollment,
        subject=erp.subject,
        kind="predicted",
        grade="A*",
        as_of=date(2026, 9, 1),
        approved_by=erp.admin,
        approved_at=timezone.now(),
    )
    GradeForecast.objects.create(
        school=erp.school,
        enrollment=erp.enrollment,
        subject=erp.subject,
        kind="predicted",
        grade="C",
        as_of=date(2026, 9, 10),  # newer, but not approved
    )
    GradeForecast.objects.create(
        school=erp.school,
        enrollment=erp.enrollment,
        subject=erp.subject,
        kind="target",
        grade="A",
        as_of=date(2026, 9, 11),
        approved_by=erp.admin,
        approved_at=timezone.now(),
    )
    payload, sources = build(erp.student)
    (group,) = payload["board"]
    assert [r["code"] for r in group["results"]] == ["0580"] and group["body"].startswith("Cambridge")
    assert payload["predicted"] == [{"subject": "Math", "grade": "A*", "as_of": "2026-09-01"}]
    assert len(sources["official"]) == 1 and len(sources["forecasts"]) == 1
    assert build(erp.student, with_predicted=False)[0]["predicted"] == []


# ------------------------------------------------------------------ issuing and checking


def test_issuing_numbers_freezes_and_answers_the_request(published):
    erp = published
    request = TranscriptRequest.objects.create(
        school=erp.school, student=erp.student, requested_by=erp.parent, purpose="University application"
    )
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    assert transcript.serial == f"TR-{timezone.localdate().year}-0001" and len(transcript.fingerprint) == 14
    request.refresh_from_db()
    assert request.status == "issued" and request.transcript == transcript
    assert AuditLog.objects.filter(action="transcript.issued").exists()
    second = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    assert second.serial.endswith("-0002")
    assert second.fingerprint != transcript.fingerprint  # the same results, a different document
    # Only the school's managers issue.
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        issue_transcript(school=erp.school, user=erp.teacher, student=erp.student)


def test_a_classmates_correction_leaves_it_current_and_the_students_own_does_not(published):
    erp = published
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    before = transcript.payload
    correct(erp, erp.classmate, 58)
    assert staleness(transcript) == {"corrected": [], "newer": []}
    correct(erp, erp.enrollment, 35)
    transcript.refresh_from_db()
    assert transcript.payload == before  # frozen
    assert staleness(transcript)["corrected"] == ["Term 1 has since been corrected."]
    page = Client().get(f"/exams/transcripts/verify/{transcript.verification_code}/").content.decode()
    assert "has since been corrected" in page


def test_an_amended_official_result_is_flagged(published):
    erp = published
    result = official(erp)
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    result.is_current = False
    result.save()
    assert staleness(transcript)["corrected"]


def test_the_public_check_shows_grades_and_initials_only(published):
    erp = published
    erp.student.last_name = "Rahman"
    erp.student.save()
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    response = Client().get(f"/exams/transcripts/verify/{transcript.verification_code}/")
    page = response.content.decode()
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    assert "A••••• R•••••" in page and transcript.serial in page and transcript.fingerprint in page
    letter = transcript.payload["years"][0]["subjects"][0]["letter"]
    assert f"<b>{letter}</b>" in page and "Math" in page
    assert "Ayesha" not in page and "2016" not in page and "Karim" not in page and "82" not in page


def test_revoke_and_reissue(published):
    erp = published
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    with pytest.raises(ValidationError):
        revoke_transcript(school=erp.school, user=erp.admin, transcript=transcript, reason="")
    new = reissue_transcript(school=erp.school, user=erp.admin, transcript=transcript)
    transcript.refresh_from_db()
    assert transcript.is_revoked and new.replaces == transcript
    page = Client().get(f"/exams/transcripts/verify/{transcript.verification_code}/").content.decode()
    assert "revoked" in page and new.serial in page


def test_the_pdf_has_every_page_numbered_with_the_serial(published):
    erp = published
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    response = login(erp.admin).get(f"/exams/transcripts/{transcript.pk}/pdf/")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    # The footer knows the page count on every page, the first included.
    from reportlab.platypus import PageBreak, Paragraph

    from core.pdf import document, styles

    calls = []
    document(
        erp.school,
        "Two pages",
        [Paragraph("one", styles()["normal"]), PageBreak(), Paragraph("two", styles()["normal"])],
        footer=lambda page, pages: calls.append((page, pages)) or f"Page {page} of {pages}",
    )
    assert calls == [(1, 2), (2, 2)]


# ------------------------------------------------------------------ who may do what


def test_the_staff_screens_are_the_managers(published):
    erp = published
    url = f"/exams/transcripts/student/{erp.student.pk}/"
    assert login(erp.teacher).get(url).status_code == 403
    assert login(erp.accountant).get("/exams/transcripts/").status_code == 403
    page = login(erp.admin).get(url).content.decode()
    assert "Preview" in page and "Term 1" in page
    login(erp.admin).post(url, {"action": "issue", "options": "1", "percent": "1"})
    assert Transcript.objects.filter(student=erp.student).count() == 1
    # Another school's manager cannot open it.
    from tests.test_analytics_foundation import add_user

    theirs = add_user(erp, "their_admin", "Administrator", school=erp.other)
    assert login(theirs).get(url).status_code == 404


def test_a_family_asks_and_downloads_only_their_own(published):
    erp = published
    client = login(erp.parent)
    client.post("/portal/transcripts/", {"purpose": "University application", "note": "Two copies"})
    assert TranscriptRequest.objects.filter(student=erp.student, status="requested").count() == 1
    client.post("/portal/transcripts/", {"purpose": "Another"})
    assert TranscriptRequest.objects.filter(student=erp.student).count() == 1  # one waits at a time
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    page = client.get("/portal/transcripts/").content.decode()
    assert transcript.serial in page
    assert client.get(f"/portal/transcripts/{transcript.pk}/pdf/").status_code == 200
    other = issue_transcript(school=erp.school, user=erp.admin, student=erp.classmate.student)
    assert client.get(f"/portal/transcripts/{other.pk}/pdf/").status_code == 404
    revoke_transcript(school=erp.school, user=erp.admin, transcript=transcript, reason="Wrong year")
    assert client.get(f"/portal/transcripts/{transcript.pk}/pdf/").status_code == 404


def test_erasure_clears_the_student_but_keeps_the_serial(published):
    erp = published
    transcript = issue_transcript(school=erp.school, user=erp.admin, student=erp.student)
    TranscriptRequest.objects.create(
        school=erp.school, student=erp.student, requested_by=erp.parent, purpose="Visa", status="declined"
    )
    from students.privacy import erase_student

    erp.student.status = Student.Status.GRADUATED
    erp.student.save()
    erp.school.retention_years = 1
    erp.school.save()
    from academics.models import AcademicYear

    AcademicYear.objects.filter(pk=erp.year.pk).update(start_date=date(2023, 1, 1), end_date=date(2023, 12, 31))
    erp.student.refresh_from_db()
    erase_student(school=erp.school, user=erp.admin, student=erp.student, confirm=erp.student.student_id)
    transcript.refresh_from_db()
    assert transcript.payload["student"] == {"name": "Erased", "student_id": "S1"}
    assert transcript.serial and transcript.fingerprint
    assert TranscriptRequest.objects.get(student=erp.student).purpose == ""
