"""Admission, roster, leaving, import and printable student documents."""

from datetime import date

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from academics.models import ClassLevel, Section
from core.models import AuditLog
from students.models import Enrollment, Guardian, Student, StudentGuardian
from students.services import change_status, import_students, next_roll_number, validate_import


def admission_payload(erp, **overrides):
    data = {
        "student_id": "S-NEW-1",
        "first_name": "Nusrat",
        "last_name": "Jahan",
        "gender": "F",
        "date_of_birth": "2016-04-02",
        "admission_date": "2026-01-05",
        "guardian_name": "Kamal Hossain",
        "guardian_phone": "01712345699",
        "guardian_relation": "father",
        "academic_year": erp.year.pk,
        "class_level": erp.level.pk,
        "section": erp.section.pk,
    }
    data.update(overrides)
    return data


def test_admission_creates_student_guardian_and_enrollment_together(admin_client, erp):
    response = admin_client.post("/students/admit/", admission_payload(erp))
    assert response.status_code == 302
    student = Student.objects.get(student_id="S-NEW-1")
    link = StudentGuardian.objects.get(student=student)
    enrollment = Enrollment.objects.get(student=student)
    assert link.is_primary and link.guardian.phone == "01712345699"
    assert enrollment.section == erp.section and enrollment.class_level == erp.level
    assert enrollment.roll_number == next_roll_number(erp.year, erp.section) - 1
    assert AuditLog.objects.filter(action="student.admitted", school=erp.school).exists()


def test_admission_is_atomic_when_the_roll_is_taken(admin_client, erp):
    before = Student.objects.count()
    response = admin_client.post("/students/admit/", admission_payload(erp, roll_number=erp.enrollment.roll_number))
    assert response.status_code == 200
    assert b"already used" in response.content
    assert Student.objects.count() == before
    assert Guardian.objects.filter(phone="01712345699").count() == 0


def test_admission_reuses_a_guardian_already_on_file_by_phone(admin_client, erp):
    existing = Guardian.objects.create(school=erp.school, full_name="Kamal Hossain", phone="01712345699")
    admin_client.post("/students/admit/", admission_payload(erp))
    assert Guardian.objects.filter(school=erp.school, phone="01712345699").count() == 1
    assert StudentGuardian.objects.filter(guardian=existing).exists()


def test_admission_does_not_reuse_a_guardian_from_another_school(admin_client, erp):
    Guardian.objects.create(school=erp.other, full_name="Someone Else", phone="01712345699")
    admin_client.post("/students/admit/", admission_payload(erp))
    assert Guardian.objects.filter(phone="01712345699").count() == 2
    assert Guardian.objects.get(school=erp.school, phone="01712345699").full_name == "Kamal Hossain"


def test_admission_rejects_a_section_from_another_class(admin_client, erp):
    other_level = ClassLevel.objects.create(school=erp.school, name="Class 2", order=2)
    other_section = Section.objects.create(school=erp.school, class_level=other_level, name="A")
    response = admin_client.post("/students/admit/", admission_payload(erp, section=other_section.pk))
    assert response.status_code == 200
    assert b"must belong to the selected class" in response.content
    assert not Student.objects.filter(student_id="S-NEW-1").exists()


def test_teacher_cannot_admit(erp):
    client = Client()
    client.force_login(erp.teacher)
    assert client.get("/students/admit/").status_code == 403
    assert client.post("/students/admit/", admission_payload(erp)).status_code == 403


@pytest.mark.parametrize(
    "query,expected",
    [("", True), ("?status=active", True), ("?status=withdrawn", False), ("?gender=M", False)],
)
def test_student_list_filters(admin_client, erp, query, expected):
    body = admin_client.get("/students/" + query).content
    assert (b"Ayesha" in body) is expected


def test_student_list_filters_by_class_and_section(admin_client, erp):
    assert b"Ayesha" in admin_client.get(f"/students/?enrollments__section={erp.section.pk}").content
    assert b"Ayesha" not in admin_client.get(f"/students/?enrollments__section={erp.other_section.pk}").content


def test_student_list_stays_within_a_query_budget(admin_client, erp, django_assert_max_num_queries):
    for n in range(2, 25):
        student = Student.objects.create(
            school=erp.school,
            student_id=f"B{n}",
            first_name=f"Pupil {n}",
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
            roll_number=n,
        )
    # The count is flat: filters and auth cost a fixed number, rows cost none.
    with django_assert_max_num_queries(20):
        assert admin_client.get("/students/").status_code == 200


def test_withdrawing_blocks_on_unpaid_fees_then_closes_the_enrollment(erp, invoice):
    with pytest.raises(ValidationError):
        change_status(
            school=erp.school,
            user=erp.admin,
            student=erp.student,
            status="withdrawn",
            effective_date=date(2026, 9, 30),
        )
    erp.student.refresh_from_db()
    assert erp.student.status == "active"

    change_status(
        school=erp.school,
        user=erp.admin,
        student=erp.student,
        status="withdrawn",
        effective_date=date(2026, 9, 30),
        reason="Family moved",
        force=True,
    )
    erp.student.refresh_from_db()
    erp.enrollment.refresh_from_db()
    assert erp.student.status == "withdrawn"
    assert erp.enrollment.status == "left"
    assert AuditLog.objects.filter(action="student.status_changed").exists()


def test_forcing_a_release_needs_fee_authority(erp, invoice):
    erp.teacher.user_permissions.clear()
    with pytest.raises(PermissionDenied):
        change_status(
            school=erp.school,
            user=erp.teacher,
            student=erp.student,
            status="withdrawn",
            effective_date=date(2026, 9, 30),
            force=True,
        )


def test_leaving_screen_is_reachable_and_rejects_active_status(admin_client, erp):
    assert admin_client.get(f"/students/{erp.student.pk}/leaving/").status_code == 200
    response = admin_client.post(
        f"/students/{erp.student.pk}/leaving/",
        {"status": "active", "effective_date": "2026-09-30"},
    )
    assert response.status_code == 200
    erp.student.refresh_from_db()
    assert erp.student.status == "active"


CSV_HEADER = (
    "student_id,first_name,gender,date_of_birth,admission_date,guardian_name,guardian_phone,class_level,section\n"
)


def test_import_preview_reports_errors_without_writing(admin_client, erp):
    upload = SimpleUploadedFile(
        "students.csv",
        (
            CSV_HEADER + "I1,Good,M,2016-01-01,2026-01-01,Guardian One,01712345611,Class 1,A\n"
            "I2,Bad,X,not-a-date,2026-01-01,,,,\n"
        ).encode(),
    )
    response = admin_client.post("/students/import/", {"file": upload})
    assert response.status_code == 200
    assert b"Row 3" in response.content
    assert Student.objects.filter(student_id="I1").count() == 0


def test_import_preview_then_confirm_creates_guardian_and_enrollment(admin_client, erp):
    upload = SimpleUploadedFile(
        "students.csv",
        (CSV_HEADER + "I1,Rafi,M,2016-01-01,2026-01-01,Guardian One,01712345611,Class 1,A\n").encode(),
    )
    preview = admin_client.post("/students/import/", {"file": upload})
    assert b"Import 1 student" in preview.content
    assert not Student.objects.filter(student_id="I1").exists()

    assert admin_client.post("/students/import/", {"confirm": "1"}).status_code == 302
    student = Student.objects.get(student_id="I1")
    assert student.primary_guardian.phone == "01712345611"
    assert student.current_enrollment.section == erp.section


def test_import_rejects_duplicate_ids_inside_one_file(erp):
    rows = [
        {
            "student_id": "D1",
            "first_name": "One",
            "gender": "M",
            "date_of_birth": "2016-01-01",
            "admission_date": "2026-01-01",
        },
        {
            "student_id": "D1",
            "first_name": "Two",
            "gender": "F",
            "date_of_birth": "2016-01-01",
            "admission_date": "2026-01-01",
        },
    ]
    _prepared, errors = validate_import(erp.school, rows)
    assert any("appears twice" in error for error in errors)


def test_invalid_import_is_atomic(erp):
    upload = SimpleUploadedFile(
        "students.csv",
        (CSV_HEADER + "S2,Valid,M,2016-01-01,2026-01-01,,,,\nS3,Invalid,X,wrong,2026-01-01,,,,\n").encode(),
    )
    with pytest.raises(ValidationError):
        import_students(erp.school, upload)
    assert Student.objects.count() == 1


def test_id_card_pdf_renders_for_one_student_and_a_whole_section(admin_client, erp):
    single = admin_client.get(f"/students/{erp.student.pk}/id-card.pdf")
    assert single.status_code == 200 and single.content.startswith(b"%PDF")
    batch = admin_client.get(f"/students/id-cards.pdf?section={erp.section.pk}")
    assert batch.status_code == 200 and batch.content.startswith(b"%PDF")


def test_export_carries_class_and_guardian_columns(admin_client, erp):
    body = admin_client.get("/students/export/").content.decode("utf-8-sig")
    assert "Guardian phone" in body
    assert "Ayesha" in body and "Class 1 - A" in body


def test_student_detail_shows_attendance_and_fee_summary(admin_client, erp, invoice):
    from attendance.models import StudentAttendance

    StudentAttendance.objects.create(
        school=erp.school, enrollment=erp.enrollment, date=date(2026, 9, 21), status="present"
    )
    body = admin_client.get(f"/students/{erp.student.pk}/").content
    assert b"Attendance" in body and b"Fees due" in body
    assert invoice.invoice_no.encode() in body


def test_student_history_and_statement_are_scoped(erp, invoice):
    other = Student.objects.create(
        school=erp.school,
        student_id="PRIV",
        first_name="Private",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    client = Client()
    client.force_login(erp.parent)
    assert client.get(f"/fees/statement/{erp.student.pk}/").status_code == 200
    assert client.get(f"/fees/statement/{other.pk}/").status_code == 404
    client.force_login(erp.admin)
    assert client.get(f"/attendance/student/{erp.student.pk}/?month=2026-09").status_code == 200
    pdf = client.get(f"/fees/statement/{erp.student.pk}/?format=pdf")
    assert pdf.content.startswith(b"%PDF")


def test_daily_summary_flags_sections_without_a_register(admin_client, erp):
    body = admin_client.get("/attendance/summary/?date=2026-09-21").content
    assert b"Not taken" in body
