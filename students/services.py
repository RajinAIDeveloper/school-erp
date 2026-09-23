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
# A roll is a position in a class register, not an identifier; four digits is already generous.
MAX_ROLL_NUMBER = 9999
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


def find_guardian(school, phone):
    """
    The guardian already on file for this number, whatever form it was written in.

    Numbers are matched on their canonical form, so a family entered once as 01712345678
    and again as +8801712345678 is one family, not two.
    """
    from .models import canonical_phone

    canonical = canonical_phone(phone)
    if not canonical:
        return None
    return Guardian.objects.filter(school=school, phone=canonical).first()


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
    guardian = find_guardian(school, phone) if phone else None
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


def _validate_guardian(extra, number, errors):
    """
    The guardian half of a row: a name, a usable Bangladeshi mobile and a real relation.

    Returns the values to write, or None when the row names no guardian at all, which is
    allowed — a school sometimes imports a roll sheet first and adds contacts later.
    """
    from messaging.services import normalize_bd_phone

    name, phone = extra.get("guardian_name", ""), extra.get("guardian_phone", "")
    relation = extra.get("guardian_relation", "") or StudentGuardian.Relation.OTHER
    if not name and not phone:
        if extra.get("guardian_relation"):
            errors.append(f"Row {number}: a guardian relation was given with no name or phone number.")
        return None
    if not name or not phone:
        errors.append(f"Row {number}: a guardian needs both a name and a mobile number.")
        return None
    if relation not in StudentGuardian.Relation.values:
        allowed = ", ".join(StudentGuardian.Relation.values)
        errors.append(f"Row {number}: '{relation}' is not a guardian relation. Use one of: {allowed}.")
        return None
    try:
        normalize_bd_phone(phone)
    except ValidationError:
        errors.append(f"Row {number}: '{phone}' is not a Bangladeshi mobile number.")
        return None
    return {"full_name": name, "phone": phone, "relation": relation}


def _validate_enrollment(school, year, extra, number, errors, claimed_rolls):
    """
    The class half of a row.

    Every part is checked here rather than at write time: an unknown class, a section that
    belongs to a different class, a roll that is not a number, and a roll already taken
    either in the database or earlier in this same file. A roll the operator supplied is
    never quietly replaced with another one.
    """
    from academics.models import ClassLevel, Section

    class_name, section_name = extra.get("class_level", ""), extra.get("section", "")
    roll_raw = extra.get("roll_number", "")
    if not class_name and not section_name:
        if roll_raw:
            errors.append(f"Row {number}: a roll number was given with no class or section.")
        return None
    if not class_name or not section_name:
        errors.append(f"Row {number}: placing a student on a roll needs both a class and a section.")
        return None
    if year is None:
        errors.append(
            f"Row {number}: this file places students in a class, but the school has no current "
            "academic year. Set one under Basic Settings, or remove the class columns."
        )
        return None
    level = ClassLevel.objects.filter(school=school, name=class_name).first()
    if level is None:
        errors.append(f"Row {number}: there is no class called '{class_name}'.")
        return None
    section = Section.objects.filter(school=school, class_level=level, name=section_name).first()
    if section is None:
        errors.append(f"Row {number}: '{class_name}' has no section '{section_name}'.")
        return None

    if roll_raw:
        if not roll_raw.isdigit():
            errors.append(f"Row {number}: roll '{roll_raw}' is not a number.")
            return None
        roll = int(roll_raw)
        if not 1 <= roll <= MAX_ROLL_NUMBER:
            errors.append(f"Row {number}: roll {roll} is outside 1 to {MAX_ROLL_NUMBER}.")
            return None
        if (section.pk, roll) in claimed_rolls:
            first = claimed_rolls[(section.pk, roll)]
            errors.append(f"Row {number}: roll {roll} in {section} is already used by row {first} of this file.")
            return None
        if Enrollment.objects.filter(academic_year=year, section=section, roll_number=roll).exists():
            errors.append(f"Row {number}: roll {roll} in {section} is already taken this year.")
            return None
    else:
        roll = next_roll_number(year, section)
        while (section.pk, roll) in claimed_rolls:
            roll += 1
        if roll > MAX_ROLL_NUMBER:
            errors.append(f"Row {number}: {section} has no free roll number left.")
            return None
    claimed_rolls[(section.pk, roll)] = number
    return {"academic_year": year, "class_level": level, "section": section, "roll_number": roll}


def validate_import(school, rows):
    """
    Return (prepared, errors) without writing anything, so the operator can see exactly
    what a file will do before committing to it.

    Everything the confirmation step will write is validated here — the student, the
    guardian, and the class placement — and the prepared rows carry the checked values.
    The write then consumes those values rather than reading the raw CSV a second time,
    so the preview and the import cannot disagree.
    """
    from academics.models import AcademicYear

    from .forms import StudentForm

    year = AcademicYear.current_for(school)
    prepared, errors, seen, claimed_rolls = [], [], set(), {}
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

        before = len(errors)
        guardian = _validate_guardian(extra, number, errors)
        enrollment = _validate_enrollment(school, year, extra, number, errors, claimed_rolls)
        if len(errors) > before:
            valid = False
        prepared.append(
            {
                "number": number,
                "form": form,
                "extra": extra,
                "valid": valid,
                "data": row,
                "guardian": guardian,
                "enrollment": enrollment,
            }
        )
    return prepared, errors


@transaction.atomic
def import_students(school, upload, user=None):
    """
    Import a validated CSV. A single bad row rejects the whole file.

    `upload` is a file, or the rows a preview already read from one.
    """
    rows = read_import_rows(upload) if hasattr(upload, "read") else list(upload)
    prepared, errors = validate_import(school, rows)
    if errors:
        raise ValidationError(errors)
    for item in prepared:
        student = item["form"].save()
        guardian_data = item["guardian"]
        if guardian_data:
            guardian = find_guardian(school, guardian_data["phone"])
            if guardian is None:
                guardian = Guardian.objects.create(
                    school=school,
                    full_name=guardian_data["full_name"],
                    phone=guardian_data["phone"],
                )
            StudentGuardian.objects.get_or_create(
                student=student,
                guardian=guardian,
                defaults={"relation": guardian_data["relation"], "is_primary": True},
            )
        placement = item["enrollment"]
        if placement:
            Enrollment.objects.create(school=school, student=student, **placement)
    if user is not None:
        AuditLog.objects.create(
            school=school, user=user, action="students.imported", description=f"{len(prepared)} students"
        )
    return len(prepared)


class ChoiceError(Exception):
    """Carries per-student messages so the choices screen can show each one in place."""

    def __init__(self, errors):
        self.errors = errors
        super().__init__(f"{len(errors)} student(s) have choices that cannot be saved")


@transaction.atomic
def save_subject_choices(*, school, user, section, academic_year, rows):
    """
    Record each student's group, main choice subjects and 4th subject for one section.

    `rows` is [(enrollment, group, chosen_subject_ids, fourth_subject_id_or_None)]. Every row
    is checked against the class's subject plan before any is saved, and one bad row rejects
    the whole submission, reported against its own student. A subject from another school, or
    one the plan does not offer that group, is refused rather than quietly stored.
    """
    from academics.models import Subject
    from examinations.subjects import check_choices, subject_plan

    assert_actor_school(user, school)
    assert_school(school, section, academic_year)
    if not user.has_perm("students.change_enrollment"):
        raise PermissionDenied
    plan = subject_plan(academic_year, section.class_level)
    if plan is None:
        raise ValidationError(
            f"{section.class_level} has no subject plan for {academic_year}, so there is nothing to choose."
        )
    board = section.class_level.uses_board_rules
    known = {subject.pk: subject for subject in Subject.objects.filter(school=school)}
    errors, prepared = {}, []
    for enrollment, group, chosen, fourth in rows:
        if enrollment.section_id != section.pk or enrollment.academic_year_id != academic_year.pk:
            raise ValidationError(
                "A student in this submission is not in this section this year. Reload and try again."
            )
        chosen = [int(pk) for pk in chosen]
        fourth = int(fourth) if fourth else None
        if any(pk not in known for pk in chosen) or (fourth and fourth not in known):
            errors[enrollment.pk] = "A chosen subject does not exist in this school."
            continue
        if fourth and not board:
            errors[enrollment.pk] = "A 4th subject applies only under the Bangladesh national curriculum."
            continue
        enrollment.group = group or ""
        enrollment.fourth_subject_id = fourth
        # Checked as if already saved, without touching the database.
        enrollment._prefetched_objects_cache = {"chosen_subjects": [known[pk] for pk in chosen]}
        problems = check_choices(enrollment, plan)
        if problems:
            errors[enrollment.pk] = " ".join(problems)
            continue
        prepared.append((enrollment, chosen))
    if errors:
        raise ChoiceError(errors)
    for enrollment, chosen in prepared:
        enrollment._prefetched_objects_cache = {}
        enrollment.save(update_fields=["group", "fourth_subject", "updated_at"])
        enrollment.chosen_subjects.set(chosen)
    AuditLog.objects.create(
        school=school,
        user=user,
        action="students.subject_choices_saved",
        description=f"{section} {academic_year}: {len(prepared)} student(s)",
    )
    return len(prepared)
