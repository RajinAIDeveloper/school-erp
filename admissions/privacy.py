"""
Applicants' data under the Personal Data Protection Act 2026: kept only as long as needed.

An application that did not lead to a place is cleared once its round has been closed for the
school's keeping period (six months unless the school says otherwise). An enrolled child's
application is cleared 90 days after enrolment: by then everything it held lives in the
student's record. Clearing removes the child's and the family's details, the documents, the
timeline and staff notes; what stays is anonymous (the class, the status reached, how the family
heard of the school, the scores) so the admissions report still adds up. Fee receipts stay, as
the accounts need them, and name only the application number.
"""

import secrets
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from core.models import AuditLog
from homework.privacy import add_months

from .models import Application, AssessmentResult

S = Application.Status
CLEARED = "Cleared"
ENROLLED_KEEP_DAYS = 90
PERSONAL = [
    "last_name",
    "name_bn",
    "religion",
    "birth_registration_no",
    "previous_school",
    "previous_class",
    "guardian_name",
    "guardian_phone",
    "guardian_email",
    "guardian_occupation",
    "father_name",
    "mother_name",
    "address",
    "sibling_details",
    "age_override_reason",
    "fee_waived_reason",
    "decision_reason",
]


def clear_application(application):
    """Remove everything personal from one application; the anonymous outline stays."""
    for document in application.documents.all():
        document.file.delete(save=False)
        document.delete()
    application.events.all().delete()
    AssessmentResult.objects.filter(application=application).update(notes="")
    Application.objects.filter(pk=application.pk).update(
        first_name=CLEARED,
        date_of_birth=date(application.date_of_birth.year, 1, 1),
        # A hash no link can produce: the family's link opens nothing from now on.
        token_hash=secrets.token_hex(32),
        consent_ip=None,
        sibling=None,
        sibling_verified_by=None,
        possible_duplicate_of=None,
        purged_at=timezone.now(),
        **dict.fromkeys(PERSONAL, ""),
    )


def due_for_clearing(school, today):
    """Applications whose keeping period is over, not yet cleared."""
    applications = Application.objects.filter(school=school, purged_at__isnull=True).select_related(
        "round_class__admission_round"
    )
    closed = [
        application.pk
        for application in applications.exclude(status=S.ENROLLED)
        if add_months(application.round_class.admission_round.closes_on, school.admissions_keep_months) < today
    ]
    cutoff = timezone.now() - timedelta(days=ENROLLED_KEEP_DAYS)
    enrolled = applications.filter(status=S.ENROLLED, enrolled_at__lt=cutoff).values_list("pk", flat=True)
    return Application.objects.filter(pk__in=[*closed, *enrolled])


def purge(school, today, *, dry_run=False):
    due = due_for_clearing(school, today)
    count = due.count()
    if dry_run or not count:
        return count
    with transaction.atomic():
        for application in due:
            clear_application(application)
        AuditLog.objects.create(
            school=school,
            action="admissions.applications_cleared",
            model=Application._meta.label,
            description=(
                f"{count} application(s) past the keeping period: {school.admissions_keep_months} months after "
                f"the round closed, or {ENROLLED_KEEP_DAYS} days after enrolment"
            ),
        )
    return count
