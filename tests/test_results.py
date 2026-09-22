"""Grid mark entry, publication side effects and printed examination documents."""

from decimal import Decimal

import pytest
from django.test import Client

from examinations.models import Exam, Mark, ResultSnapshot, UnlockRequest
from examinations.services import MarkEntryError, publish_exam, save_mark, save_marks
from messaging.models import SMSMessage
from students.models import Enrollment, Student


def extra_student(erp, roll, name="Second"):
    student = Student.objects.create(
        school=erp.school,
        student_id=f"G{roll}",
        first_name=name,
        gender="M",
        date_of_birth="2016-01-01",
        admission_date="2026-01-01",
    )
    return Enrollment.objects.create(
        school=erp.school,
        student=student,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=roll,
    )


def grid_payload(erp, rows):
    payload = {"schedule": erp.schedule.pk, "section": erp.section.pk}
    for enrollment, score, absent, version in rows:
        payload[f"{enrollment.pk}-score"] = score
        payload[f"{enrollment.pk}-version"] = version
        if absent:
            payload[f"{enrollment.pk}-absent"] = "on"
    return payload


def test_grid_saves_a_whole_class_in_one_submission(erp):
    second = extra_student(erp, 2)
    client = Client()
    client.force_login(erp.teacher)
    response = client.post(
        "/exams/marks/", grid_payload(erp, [(erp.enrollment, "80", False, 0), (second, "65", False, 0)])
    )
    assert response.status_code == 302
    assert Mark.objects.count() == 2
    assert {str(m.marks_obtained) for m in Mark.objects.all()} == {"80.00", "65.00"}


def test_grid_saves_nothing_when_one_row_is_invalid(erp):
    second = extra_student(erp, 2)
    client = Client()
    client.force_login(erp.teacher)
    response = client.post(
        "/exams/marks/",
        grid_payload(erp, [(erp.enrollment, "80", False, 0), (second, "150", False, 0)]),
    )
    assert response.status_code == 200
    assert b"Nothing was saved" in response.content
    assert not Mark.objects.exists()


def test_grid_reports_a_non_numeric_entry_against_its_own_row(erp):
    client = Client()
    client.force_login(erp.teacher)
    response = client.post("/exams/marks/", grid_payload(erp, [(erp.enrollment, "eighty", False, 0)]))
    assert response.status_code == 200
    assert b"is not a number" in response.content
    assert not Mark.objects.exists()


def test_grid_records_an_absence_without_a_score(erp):
    client = Client()
    client.force_login(erp.teacher)
    client.post("/exams/marks/", grid_payload(erp, [(erp.enrollment, "", True, 0)]))
    mark = Mark.objects.get()
    assert mark.is_absent and mark.marks_obtained is None


def test_grid_leaves_blank_rows_alone(erp):
    second = extra_student(erp, 2)
    client = Client()
    client.force_login(erp.teacher)
    client.post("/exams/marks/", grid_payload(erp, [(erp.enrollment, "70", False, 0), (second, "", False, 0)]))
    assert Mark.objects.count() == 1


def test_grid_respects_a_stale_version(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    client = Client()
    client.force_login(erp.teacher)
    response = client.post("/exams/marks/", grid_payload(erp, [(erp.enrollment, "90", False, 0)]))
    assert response.status_code == 200
    assert b"Reload before saving" in response.content
    assert Mark.objects.get().marks_obtained == Decimal("70.00")


def test_grid_service_reports_errors_per_student(erp):
    second = extra_student(erp, 2)
    with pytest.raises(MarkEntryError) as caught:
        save_marks(
            user=erp.teacher,
            schedule=erp.schedule,
            section=erp.section,
            rows=[(erp.enrollment, Decimal(80), False, 0), (second, Decimal(500), False, 0)],
        )
    assert second.pk in caught.value.errors
    assert not Mark.objects.exists()


def test_grid_is_read_only_once_results_are_published(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    publish_exam(erp.exam, erp.admin)
    client = Client()
    client.force_login(erp.teacher)
    body = client.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}").content
    assert b"marks are locked" in body
    assert client.post("/exams/marks/", grid_payload(erp, [(erp.enrollment, "90", False, 1)])).status_code == 403


def test_grid_shows_progress_for_the_class(erp):
    extra_student(erp, 2)
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    client = Client()
    client.force_login(erp.teacher)
    body = client.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}").content
    assert b"1 of 2 entered" in body
    assert b"1 still blank" in body


def test_report_card_pdf_has_school_and_attendance(erp):
    from attendance.models import StudentAttendance

    StudentAttendance.objects.create(school=erp.school, enrollment=erp.enrollment, date="2026-09-21", status="present")
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    client = Client()
    client.force_login(erp.admin)
    response = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert len(response.content) > 2000


def test_bulk_report_cards_cover_the_section(erp):
    extra_student(erp, 2)
    for enrollment in Enrollment.objects.filter(section=erp.section):
        save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=enrollment, score=Decimal(60))
    publish_exam(erp.exam, erp.admin)
    client = Client()
    client.force_login(erp.admin)
    response = client.get(f"/exams/report-cards.pdf?exam={erp.exam.pk}&section={erp.section.pk}")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")


def test_admit_cards_render_for_a_section(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.get(f"/exams/admit-cards.pdf?exam={erp.exam.pk}&section={erp.section.pk}")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")


def test_a_teacher_cannot_print_another_section(erp):
    client = Client()
    client.force_login(erp.teacher)
    assert client.get(f"/exams/admit-cards.pdf?exam={erp.exam.pk}&section={erp.other_section.pk}").status_code == 403


def test_exam_routine_lists_papers(admin_client, erp):
    body = admin_client.get(f"/exams/{erp.exam.pk}/routine/").content
    assert b"Math" in body
    assert admin_client.get(f"/exams/{erp.exam.pk}/routine/?format=pdf").content.startswith(b"%PDF")


def test_exam_detail_shows_mark_progress_and_publish_control(admin_client, erp):
    body = admin_client.get(f"/exams/{erp.exam.pk}/").content
    assert b"Marks entered" in body
    assert b"Publish results" in body
    assert b"Admit cards" in body


def test_progress_report_lists_published_exams_only(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    client = Client()
    client.force_login(erp.parent)
    body = client.get(f"/exams/progress/{erp.student.pk}/").content
    assert b"Term 1" not in body

    publish_exam(erp.exam, erp.admin)
    body = client.get(f"/exams/progress/{erp.student.pk}/").content
    assert b"Term 1" in body
    assert client.get(f"/exams/progress/{erp.student.pk}/?format=pdf").content.startswith(b"%PDF")


def test_progress_report_is_scoped_to_visible_students(erp):
    other = Student.objects.create(
        school=erp.school,
        student_id="P9",
        first_name="Private",
        gender="M",
        date_of_birth="2016-01-01",
        admission_date="2026-01-01",
    )
    client = Client()
    client.force_login(erp.parent)
    assert client.get(f"/exams/progress/{other.pk}/").status_code == 404


def test_verification_page_names_no_student_or_mark(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    snapshot = ResultSnapshot.objects.get()
    response = Client().get(f"/exams/verify/{snapshot.verification_code}/")
    assert response.status_code == 200
    body = response.content
    assert b"Test School" in body
    assert b"current, published" in body
    # The privacy property: no child is named and no mark is shown.
    assert b"Ayesha" not in body
    assert b"GPA" not in body and b"5.00" not in body


def test_verification_flags_a_superseded_version(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    publish_exam(erp.exam, erp.admin)
    first = ResultSnapshot.objects.get()
    unlock = UnlockRequest.objects.create(
        school=erp.school, schedule=erp.schedule, requested_by=erp.teacher, reason="Re-totalled"
    )
    from examinations.services import review_unlock

    review_unlock(unlock, erp.admin, True)
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80), expected_version=1)
    body = Client().get(f"/exams/verify/{first.verification_code}/").content
    assert b"superseded" in body


def test_result_sms_is_queued_once_per_publication(erp, django_capture_on_commit_callbacks):
    erp.school.notify_results_sms = True
    erp.school.save()
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    with django_capture_on_commit_callbacks(execute=True):
        publish_exam(erp.exam, erp.admin)
    message = SMSMessage.objects.get()
    assert "Term 1" in message.body and "Ayesha" in message.body
    exam = Exam.objects.get(pk=erp.exam.pk)
    assert message.dedupe_key == f"results:{exam.pk}:{erp.enrollment.pk}:{exam.publication_version}"

    # Publishing again is a no-op, so no second message.
    with django_capture_on_commit_callbacks(execute=True):
        publish_exam(exam, erp.admin)
    assert SMSMessage.objects.count() == 1


def test_no_result_sms_unless_the_school_asks_for_it(erp, django_capture_on_commit_callbacks):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    with django_capture_on_commit_callbacks(execute=True):
        publish_exam(erp.exam, erp.admin)
    assert not SMSMessage.objects.exists()
