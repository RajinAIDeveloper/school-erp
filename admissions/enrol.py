"""
Enrolment: an accepted place becomes a student, without anything typed twice.

Enrolment is a deliberate step taken by the school's managers, one class at a time. The child,
the guardian and the documents come across from the application, through the same `admit`
the office uses for any new student. Two things are never done silently:

- A guardian already on file with the same mobile number is attached only when staff confirm
  it is the same family. A mistyped number would otherwise put the child in a stranger's family,
  with their fees, messages and portal.
- A child enrolled into a year that is not the current one is an active student at once: they
  count on dashboards and receive school-wide messages before the year starts. Staff confirm it.

The documents and photo are copied, so clearing the application later leaves the student's
copies in place. The consent given on the application is carried over as a consent record.
"""

from pathlib import PurePath

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school
from core.models import School

from . import services
from .models import DOCUMENT_LABELS, Application, ApplicationDocument

S = Application.Status


def family_match(application):
    """The guardian already on file with this application's mobile number, and their children."""
    from students.services import find_guardian

    guardian = find_guardian(application.school, application.guardian_phone)
    if guardian is None:
        return None
    children = [link.student for link in guardian.student_links.select_related("student")]
    return {"guardian": guardian, "children": children}


def section_load(section, academic_year):
    from students.models import Enrollment

    return Enrollment.objects.filter(
        section=section, academic_year=academic_year, status=Enrollment.Status.ENROLLED
    ).count()


def _copy(field, name):
    with field.open("rb") as handle:
        return ContentFile(handle.read(), name=name)


@transaction.atomic
def enrol(*, user, application, section, roll_number=None, same_family=False, future_ok=False, today=None):
    """Admit the child of an accepted application into `section`. Returns the new student."""
    from messaging.notifications import notify_admission
    from students.models import GuardianConsent, Student, StudentDocument
    from students.services import admit

    school = application.school
    assert_actor_school(user, school)
    if not (user.has_perm("admissions.decide_application") and user.has_perm("students.add_student")):
        raise PermissionDenied
    assert_school(school, section)
    today = today or timezone.localdate()
    locked = (
        Application.objects.select_for_update()
        .select_related("round_class__admission_round__academic_year", "round_class__class_level")
        .get(pk=application.pk)
    )
    if locked.current_status != S.ACCEPTED:
        raise ValidationError(f"{locked.reference} is {locked.get_current_status_display().lower()}, not accepted.")
    round_class = locked.round_class
    year = round_class.admission_round.academic_year
    if section.class_level_id != round_class.class_level_id:
        raise ValidationError(f"{section} is not a section of {round_class.class_level}.")
    if not year.is_current and not future_ok:
        raise ValidationError(
            f"{year} is not the current year. A child enrolled now is an active student at once; confirm to go ahead."
        )
    match = family_match(locked)
    if match and not same_family:
        raise ValidationError(
            f"{locked.guardian_phone} is the number of {match['guardian'].full_name}, already a guardian at the "
            "school. Confirm it is the same family, or correct the number on the application."
        )

    School.objects.select_for_update().get(pk=school.pk)
    student = Student(
        student_id=Student.next_student_id(school, year.name),
        first_name=locked.first_name,
        last_name=locked.last_name,
        name_bn=locked.name_bn,
        gender=locked.gender,
        date_of_birth=locked.date_of_birth,
        religion=locked.religion,
        birth_registration_no=locked.birth_registration_no,
        previous_school=locked.previous_school,
        father_name=locked.father_name,
        mother_name=locked.mother_name,
        present_address=locked.address,
        admission_date=today,
    )
    student, guardian, enrollment = admit(
        school=school,
        user=user,
        student=student,
        guardian_data={
            "full_name": locked.guardian_name,
            "phone": locked.guardian_phone,
            "email": locked.guardian_email,
            "occupation": locked.guardian_occupation,
            "relation": locked.guardian_relation,
        },
        enrollment_data={"academic_year": year, "section": section, "roll_number": roll_number},
    )

    documents = locked.documents.exclude(status=ApplicationDocument.Status.REJECTED)
    photo = documents.filter(kind="photo", file_kind="jpeg").order_by("-status", "-pk").first()
    if photo is not None:
        student.photo.save(f"{student.pk}.jpg", _copy(photo.file, "photo.jpg"), save=True)
    for document in documents:
        suffix = PurePath(document.file.name).suffix.lower()
        StudentDocument.objects.create(
            school=school,
            student=student,
            title=f"{DOCUMENT_LABELS[document.kind]} (application {locked.reference})",
            file=_copy(document.file, f"document{suffix}"),
            uploaded_by=user,
        )
    GuardianConsent.objects.create(
        school=school,
        student=student,
        purpose=GuardianConsent.Purpose.RECORDS,
        given=True,
        method=GuardianConsent.Method.APPLICATION,
        recorded_by=user,
        note=(
            f"Wording {locked.consent_version}, given {timezone.localtime(locked.consent_at):%d %b %Y} "
            f"with application {locked.reference}."
        ),
    )
    Application.objects.filter(pk=locked.pk).update(student=student)
    services.transition(
        locked,
        S.ENROLLED,
        user=user,
        reason=f"Student {student.student_id} in {section}, roll {enrollment.roll_number}.",
    )
    transaction.on_commit(lambda: notify_admission(student, enrollment))
    return student
