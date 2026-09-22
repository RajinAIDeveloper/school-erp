"""
Student lifecycle operations.

Admission, import, promotion and leaving are the points where a student record changes
shape. They all run in one transaction and keep history: a student who leaves keeps every
past enrollment, and a promotion adds a year rather than overwriting one.
"""

import csv
from io import StringIO

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school
from core.models import AuditLog, School

from .models import Enrollment, Guardian, Student, StudentGuardian

MAX_IMPORT_ROWS = 5000
IMPORT_COLUMNS = [
    "student_id",
    "first_name",
    "last_name",
    "name_bn",
    "gender",
    "date_of_birth",
    "birth_registration_no",
    "blood_group",
    "religion",
    "phone",
    "email",
    "present_address",
    "permanent_address",
    "previous_school",
    "admission_date",
    "status",
]
IMPORT_EXTRA_COLUMNS = ["guardian_name", "guardian_phone", "guardian_relation", "class_level", "section", "roll_number"]


def next_roll_number(academic_year, section):
    highest = (
        Enrollment.objects.filter(academic_year=academic_year, section=section)
        .order_by("-roll_number")
        .values_list("roll_number", flat=True)
        .first()
    )
    return (highest or 0) + 1


@transaction.atomic
def admit(*, school, user, student, guardian_data, enrollment_data):
    """
    Create the student, attach a guardian (reusing one already on file with the same
    mobile number) and place them on a roll. Either all three land or none do.
    """
    assert_actor_school(user, school)
    if not user.has_perm("students.add_student"):
        raise PermissionDenied
    student.school = school
    student.full_clean(exclude=["school"])
    student.save()

    phone = (guardian_data.get("phone") or "").strip()
    guardian = Guardian.objects.filter(school=school, phone=phone).first() if phone else None
    if guardian is None:
        guardian = Guardian.objects.create(
            school=school,
            full_name=guardian_data["full_name"],
            phone=phone,
            email=guardian_data.get("email", ""),
            nid=guardian_data.get("nid", ""),
            occupation=guardian_data.get("occupation", ""),
            address=student.present_address,
        )
    StudentGuardian.objects.create(
        student=student, guardian=guardian, relation=guardian_data.get("relation", "other"), is_primary=True
    )

    year, section = enrollment_data["academic_year"], enrollment_data["section"]
    assert_school(school, year, section)
    enrollment = Enrollment.objects.create(
        school=school,
        student=student,
        academic_year=year,
        class_level=section.class_level,
        section=section,
        roll_number=enrollment_data.get("roll_number") or next_roll_number(year, section),
    )
    AuditLog.objects.create(
        school=school,
        user=user,
        action="student.admitted",
        model=student._meta.label,
        object_id=str(student.pk),
        description=f"{student} into {section} roll {enrollment.roll_number}",
    )
    return student, guardian, enrollment


@transaction.atomic
def change_status(*, school, user, student, status, effective_date, reason="", force=False):
    """
    Graduate, transfer out or withdraw a student. The current enrollment is closed so the
    class roll and attendance registers stop including them from this date.
    """
    assert_actor_school(user, school)
    assert_school(school, student)
    if not user.has_perm("students.change_student"):
        raise PermissionDenied
    if status not in Student.Status.values or status == Student.Status.ACTIVE:
        raise ValidationError("Choose graduated, transferred or withdrawn.")

    from fees.models import FeeInvoice

    outstanding = sum(
        (invoice.balance for invoice in FeeInvoice.objects.filter(student=student).exclude(status="cancelled")),
        start=0,
    )
    if outstanding > 0 and not force:
        raise ValidationError(
            f"{student} still owes {outstanding}. Settle the account, or an administrator may release the record."
        )
    if outstanding > 0 and not user.has_perm("fees.change_feeinvoice"):
        raise PermissionDenied("Only staff who manage fees may release a student with an unpaid balance.")

    student.status = status
    student.save(update_fields=["status", "updated_at"])
    closed = Enrollment.objects.filter(student=student, status=Enrollment.Status.ENROLLED).update(
        status=Enrollment.Status.LEFT, updated_at=timezone.now()
    )
    AuditLog.objects.create(
        school=school,
        user=user,
        action="student.status_changed",
        model=student._meta.label,
        object_id=str(student.pk),
        description=f"{status} on {effective_date}; {closed} enrollment(s) closed; {reason}".strip("; "),
    )
    return student


@transaction.atomic
def promote(school, source_year, source_section, target_year, target_section):
    assert_school(school, source_year, source_section, target_year, target_section)
    if target_year.start_date <= source_year.start_date:
        raise ValidationError("Target year must follow source year.")
    School.objects.select_for_update().get(pk=school.pk)
    rows = list(
        Enrollment.objects.filter(
            school=school,
            academic_year=source_year,
            section=source_section,
            status="enrolled",
            student__status="active",
        ).order_by("roll_number")
    )
    if Enrollment.objects.filter(academic_year=target_year, student_id__in=[e.student_id for e in rows]).exists():
        raise ValidationError("Some students already have a target-year enrollment; no records changed.")
    roll = next_roll_number(target_year, target_section) - 1
    for e in rows:
        roll += 1
        Enrollment.objects.create(
            school=school,
            student=e.student,
            academic_year=target_year,
            section=target_section,
            class_level=target_section.class_level,
            roll_number=roll,
        )
        e.status = "promoted"
        e.save(update_fields=["status", "updated_at"])
    return len(rows)


def read_import_rows(upload):
    """Decode and sanity-check the upload before anything touches the database."""
    if upload.size > 5 * 1024 * 1024:
        raise ValidationError("Maximum import size is 5 MB.")
    try:
        rows = list(csv.DictReader(StringIO(upload.read().decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValidationError("Upload a UTF-8 CSV file.") from exc
    if not rows or len(rows) > MAX_IMPORT_ROWS:
        raise ValidationError(f"Import requires between 1 and {MAX_IMPORT_ROWS} rows.")
    return rows


def validate_import(school, rows):
    """
    Return (prepared, errors) without writing anything, so the operator can see exactly
    what a file will do before committing to it.
    """
    from .forms import StudentForm

    prepared, errors, seen = [], [], set()
    for number, raw in enumerate(rows, start=2):
        row = {key: (value or "").strip() for key, value in raw.items() if key in IMPORT_COLUMNS}
        # A roll sheet rarely carries a status column; everyone on it is a current student.
        if not row.get("status"):
            row["status"] = Student.Status.ACTIVE
        extra = {key: (raw.get(key) or "").strip() for key in IMPORT_EXTRA_COLUMNS}
        form = StudentForm(row, school=school)
        valid = form.is_valid()
        student_id = row.get("student_id", "")
        if student_id in seen:
            errors.append(f"Row {number}: student ID {student_id} appears twice in this file.")
            valid = False
        seen.add(student_id)
        if not valid:
            for field, messages in form.errors.items():
                errors.append(f"Row {number} ({field}): {' '.join(messages)}")
        prepared.append({"number": number, "form": form, "extra": extra, "valid": valid, "data": row})
    return prepared, errors


@transaction.atomic
def import_students(school, upload, user=None):
    """Import a validated CSV. A single bad row rejects the whole file."""
    rows = read_import_rows(upload)
    prepared, errors = validate_import(school, rows)
    if errors:
        raise ValidationError(errors)
    from academics.models import AcademicYear, ClassLevel, Section

    year = AcademicYear.current_for(school)
    for item in prepared:
        student = item["form"].save()
        extra = item["extra"]
        if extra.get("guardian_name") and extra.get("guardian_phone"):
            guardian = Guardian.objects.filter(school=school, phone=extra["guardian_phone"]).first()
            if guardian is None:
                guardian = Guardian.objects.create(
                    school=school, full_name=extra["guardian_name"], phone=extra["guardian_phone"]
                )
            StudentGuardian.objects.get_or_create(
                student=student,
                guardian=guardian,
                defaults={"relation": extra.get("guardian_relation") or "other", "is_primary": True},
            )
        if year and extra.get("class_level") and extra.get("section"):
            level = ClassLevel.objects.filter(school=school, name=extra["class_level"]).first()
            section = (
                Section.objects.filter(school=school, class_level=level, name=extra["section"]).first()
                if level
                else None
            )
            if section:
                roll = extra.get("roll_number")
                Enrollment.objects.create(
                    school=school,
                    student=student,
                    academic_year=year,
                    class_level=level,
                    section=section,
                    roll_number=int(roll) if roll.isdigit() else next_roll_number(year, section),
                )
    if user is not None:
        AuditLog.objects.create(
            school=school, user=user, action="students.imported", description=f"{len(prepared)} students"
        )
    return len(prepared)
