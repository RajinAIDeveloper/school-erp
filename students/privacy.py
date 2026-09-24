"""
Handling children's data as the Personal Data Protection Act 2026 expects: a guardian's
consent recorded per purpose, identity numbers shown only to those who need them, and a
former student's personal data erased once the school's retention period has passed.

Erasure keeps what the school must keep: marks, attendance, fees and the ledger stay, tied
to the student ID, with the name and personal details removed from all of them.
"""

from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from core.access import assert_actor_school, assert_school, is_manager, students_for
from core.models import AuditLog

from .models import Enrollment, GuardianConsent, Student, StudentGuardian

ERASED = "Erased"


# ------------------------------------------------------------------ consent


def current_consents(student):
    """{purpose: the latest record} for one student."""
    latest = {}
    for record in GuardianConsent.objects.filter(student=student).select_related("recorded_by"):
        latest.setdefault(record.purpose, record)  # newest first
    return latest


def has_consent(student, purpose):
    record = current_consents(student).get(purpose)
    return bool(record and record.given)


@transaction.atomic
def record_consent(*, user, student, purpose, given, method="form", note=""):
    """
    Record consent given or withdrawn. Staff with the student's record may record it from a
    signed form; a guardian may give or withdraw it for their own child in the portal.
    """
    assert_actor_school(user, student.school)
    if purpose not in GuardianConsent.Purpose.values or method not in GuardianConsent.Method.values:
        raise ValidationError("Choose a purpose and how the consent was given.")
    own_child = students_for(user, student.school).filter(pk=student.pk).exists()
    if method == GuardianConsent.Method.PORTAL:
        if not (hasattr(user, "guardian_profile") and own_child):
            raise PermissionDenied("Only the child's guardian can give or withdraw consent in the portal.")
    elif not user.has_perm("students.change_student"):
        raise PermissionDenied
    record = GuardianConsent.objects.create(
        school=student.school,
        student=student,
        purpose=purpose,
        given=bool(given),
        method=method,
        recorded_by=user,
        note=(note or "")[:200],
    )
    AuditLog.objects.create(
        school=student.school,
        user=user,
        action="consent.recorded",
        model=Student._meta.label,
        object_id=str(student.pk),
        description=f"{purpose}: {'given' if given else 'withdrawn or refused'} ({method})",
    )
    return record


# ------------------------------------------------------------------ who sees identity numbers


def may_see_identity(user):
    """Birth registration and NID numbers are for the school's managers only."""
    return is_manager(user)


def log_sensitive(request, what, detail=""):
    """Leave a trace whenever restricted data is opened or exported."""
    AuditLog.objects.create(
        school=request.school,
        user=request.user,
        action="privacy.viewed",
        description=f"{what}{': ' + detail if detail else ''}"[:250],
    )


# ------------------------------------------------------------------ retention and erasure


def left_on(student):
    """The end of the last year the student was enrolled, or None if never enrolled."""
    return Enrollment.objects.filter(student=student).aggregate(end=Max("academic_year__end_date"))["end"]


def erasable(student, today=None):
    """Whether the retention period has passed for a student who has left."""
    if student.status == Student.Status.ACTIVE or student.first_name == ERASED:
        return False
    today = today or timezone.localdate()
    ended = left_on(student) or student.admission_date
    years = student.school.retention_years
    try:
        due = ended.replace(year=ended.year + years)
    except ValueError:  # 29 February
        due = ended.replace(year=ended.year + years, day=28)
    return due <= today


def due_for_erasure(school, today=None):
    students = Student.objects.filter(school=school).exclude(status=Student.Status.ACTIVE).exclude(first_name=ERASED)
    return [s for s in students if erasable(s, today)]


def _scrub_payload(payload, keys):
    changed = dict(payload)
    for key in keys:
        if key in changed:
            changed[key] = ERASED if key in ("student", "student_first") else ""
    return changed


def _erase_account(user):
    if user is None:
        return
    user.is_active = False
    user.username = f"erased-{user.pk}"
    user.first_name = user.last_name = user.email = user.phone = ""
    user.set_unusable_password()
    user.save()


@transaction.atomic
def erase_student(*, school, user, student, confirm):
    """
    Remove a former student's personal data once the retention period has passed.

    `confirm` must repeat the student ID. Guardians are erased too unless another child at
    the school still links to them.
    """
    from examinations.models import AccessArrangement, CombinedSnapshot, ResultSnapshot, SeriesCandidate
    from finance.models import JournalEntry
    from messaging.models import SMSMessage

    assert_actor_school(user, school)
    assert_school(school, student)
    if not (is_manager(user) and user.has_perm("students.delete_student")):
        raise PermissionDenied
    student = Student.objects.select_for_update().get(pk=student.pk)
    if (confirm or "").strip() != student.student_id:
        raise ValidationError("Type the student ID to confirm the erasure.")
    if not erasable(student):
        raise ValidationError("This student's retention period has not passed yet.")

    name, name_bn = student.full_name, student.name_bn
    erased_phones = set()

    # The student's own record.
    if student.photo:
        student.photo.delete(save=False)
    for field in (
        "last_name",
        "name_bn",
        "birth_registration_no",
        "blood_group",
        "religion",
        "phone",
        "email",
        "present_address",
        "permanent_address",
        "previous_school",
        "notes",
        "father_name",
        "father_name_bn",
        "father_nid",
        "mother_name",
        "mother_name_bn",
        "mother_nid",
        "birth_place",
        "nationality",
        "previous_roll",
        "previous_registration_no",
        "board_registration_no",
        "unique_id",
        "result_code",
    ):
        setattr(student, field, "")
    student.first_name = ERASED
    student.date_of_birth = date(student.date_of_birth.year, 1, 1)
    student.photo = ""
    student.save()
    _erase_account(student.user)
    for document in student.documents.all():
        document.file.delete(save=False)
        document.delete()
    student.consents.all().delete()
    # Homework they handed in, and what was written about it.
    from homework.privacy import erase_homework

    erase_homework(student)

    # Guardians who have no other child here.
    for link in StudentGuardian.objects.filter(student=student).select_related("guardian"):
        guardian = link.guardian
        link.delete()
        if not StudentGuardian.objects.filter(guardian=guardian).exists():
            if guardian.phone:
                erased_phones.add(guardian.phone)
            type(guardian).objects.filter(pk=guardian.pk).update(
                full_name="Erased guardian", phone="", email="", nid="", occupation="", address=""
            )
            _erase_account(guardian.user)

    # Frozen documents: results and certificates keep their numbers, not the child's details.
    identity = ("student", "student_bn", "student_first", "father_name", "mother_name", "date_of_birth")
    for model in (ResultSnapshot, CombinedSnapshot):
        for snapshot in model.objects.filter(enrollment__student=student):
            model.objects.filter(pk=snapshot.pk).update(payload=_scrub_payload(snapshot.payload, identity))
    certificate_keys = identity + ("father", "father_bn", "mother", "mother_bn", "board_registration_no")
    for certificate in student.certificates.all():
        type(certificate).objects.filter(pk=certificate.pk).update(
            payload=_scrub_payload(certificate.payload, certificate_keys)
        )
    # Transcripts keep their serial and fingerprint; the student block goes, and so does what
    # the family wrote when asking for one.
    from examinations.models import Transcript, TranscriptRequest

    for transcript in Transcript.objects.filter(student=student):
        payload = dict(transcript.payload)
        payload["student"] = {"name": ERASED, "student_id": student.student_id}
        Transcript.objects.filter(pk=transcript.pk).update(payload=payload)
    TranscriptRequest.objects.filter(student=student).update(purpose="", note="", decline_reason="")
    # The admission application the child came through, if any.
    from admissions.models import Application
    from admissions.privacy import clear_application

    for application in Application.objects.filter(student=student, purged_at__isnull=True):
        clear_application(application)
    SeriesCandidate.objects.filter(student=student).update(certificate_name="", uci="")
    AccessArrangement.objects.filter(candidate__student=student).delete()

    # Messages and ledger narrations that named the child.
    for message in SMSMessage.objects.filter(school=school, body__contains=name):
        SMSMessage.objects.filter(pk=message.pk).update(body=message.body.replace(name, f"[{student.student_id}]"))
    # Messages to a guardian who has been erased keep the fact they were sent, not to whom.
    SMSMessage.objects.filter(school=school, phone__in=erased_phones).update(recipient_name="", phone="")
    for entry in JournalEntry.objects.filter(school=school, narration__contains=name):
        JournalEntry.objects.filter(pk=entry.pk).update(narration=entry.narration.replace(name, student.student_id))
    if name_bn:
        for message in SMSMessage.objects.filter(school=school, body__contains=name_bn):
            SMSMessage.objects.filter(pk=message.pk).update(
                body=message.body.replace(name_bn, f"[{student.student_id}]")
            )

    AuditLog.objects.create(
        school=school,
        user=user,
        action="student.erased",
        model=Student._meta.label,
        object_id=str(student.pk),
        description=f"Personal data of {student.student_id} erased after the retention period",
    )
    return student
