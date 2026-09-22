import csv
from io import StringIO

from django.core.exceptions import ValidationError
from django.db import transaction

from core.access import assert_school
from core.models import School

from .forms import StudentForm
from .models import Enrollment


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
    roll = (
        Enrollment.objects.filter(academic_year=target_year, section=target_section)
        .order_by("-roll_number")
        .values_list("roll_number", flat=True)
        .first()
        or 0
    )
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


@transaction.atomic
def import_students(school, upload):
    if upload.size > 5 * 1024 * 1024:
        raise ValidationError("Maximum import size is 5 MB.")
    try:
        rows = list(csv.DictReader(StringIO(upload.read().decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValidationError("Upload a UTF-8 CSV file.") from exc
    if not rows or len(rows) > 5000:
        raise ValidationError("Import requires between 1 and 5000 rows.")
    forms, errors, seen = [], [], set()
    for number, row in enumerate(rows, 2):
        row = {k: v for k, v in row.items() if k not in ("user", "photo")}
        form = StudentForm(row, school=school)
        valid = form.is_valid()
        sid = row.get("student_id")
        if sid in seen:
            errors.append(f"Row {number}: duplicate student ID.")
        if not valid:
            errors.append(f"Row {number}: {form.errors.as_text()}")
        seen.add(sid)
        forms.append(form)
    if errors:
        raise ValidationError(errors)
    for form in forms:
        form.save()
    return len(forms)
