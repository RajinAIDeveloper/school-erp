"""
Children's homework under the Personal Data Protection Act: files are kept only as long as the
school says, and erasing a former student takes their work with them.
"""

from datetime import date

from django.db import transaction

from .models import Submission, SubmissionFile


def erase_homework(student):
    """Delete a student's handed-in files and clear what they and their teachers wrote."""
    for page in SubmissionFile.objects.filter(submission__enrollment__student=student):
        page.delete()
    Submission.objects.filter(enrollment__student=student).update(note="", feedback="", reason="")


def add_months(day, months):
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    last = [
        31,
        29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ]
    return date(year, month, min(day.day, last[month - 1]))


def files_due_for_deletion(school, today):
    """Hand-in files of academic years that ended more than the school's keeping period ago."""
    from academics.models import AcademicYear

    years = [
        year.pk
        for year in AcademicYear.objects.filter(school=school)
        if add_months(year.end_date, school.homework_files_months) < today
    ]
    return SubmissionFile.objects.filter(school=school, submission__task__academic_year_id__in=years)


def purge_files(school, today, *, dry_run=False):
    """Delete the files past their keeping period. The records of the work stay."""
    from core.models import AuditLog

    due = files_due_for_deletion(school, today)
    count = due.count()
    if dry_run or not count:
        return count
    with transaction.atomic():
        for page in due:
            page.delete()
        AuditLog.objects.create(
            school=school,
            action="homework.files_purged",
            model=SubmissionFile._meta.label,
            description=f"{count} handed-in file(s) past the {school.homework_files_months}-month keeping period",
        )
    return count
