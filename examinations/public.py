"""
Looking up a published result without signing in.

Off unless the school switches it on. A family enters the student ID and the result code
printed on the admit card: a random code, not the date of birth, which anyone who knows the
child could guess. Repeated wrong attempts from one address, or for one student from any
address, are turned away for a while, and only published results are ever shown.
"""

import hashlib
import secrets

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import F

from core.access import assert_actor_school, assert_school
from core.models import AuditLog

from .models import CombinedSnapshot, ResultSnapshot

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I, which families misread
ATTEMPTS = 10
WINDOW = 15 * 60


def new_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalise(code):
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def ensure_codes(students):
    """Give each student a result code if they have none. Existing codes are never changed here."""
    from students.models import Student

    for student in students:
        if student.result_code:
            continue
        for _ in range(5):
            student.result_code = new_code()
            try:
                with transaction.atomic():
                    Student.objects.filter(pk=student.pk, result_code="").update(result_code=student.result_code)
                break
            except IntegrityError:
                continue
        student.refresh_from_db(fields=["result_code"])


@transaction.atomic
def reissue_code(*, user, student):
    """A new code, for when a card with the old one is lost or the code has been shared."""
    assert_actor_school(user, student.school)
    assert_school(student.school, student)
    if not user.has_perm("students.change_student"):
        raise PermissionDenied
    student.result_code = ""
    student.save(update_fields=["result_code", "updated_at"])
    ensure_codes([student])
    AuditLog.objects.create(
        school=student.school,
        user=user,
        action="student.result_code_reissued",
        model=student._meta.label,
        object_id=str(student.pk),
        description=f"{student}: result code reissued",
    )
    return student.result_code


def _key(address, school):
    return "result-lookup:" + hashlib.sha256(f"{address}:{school.pk}".encode()).hexdigest()


def _student_key(student_id, school):
    """Wrong codes for one student, from anywhere: guessing from many addresses meets this limit."""
    student_id = (student_id or "").strip().casefold()
    return "result-lookup-student:" + hashlib.sha256(f"{school.pk}:{student_id}".encode()).hexdigest()


def throttled(address, school, student_id=""):
    if cache.get(_key(address, school), 0) >= ATTEMPTS:
        return True
    return bool(student_id.strip()) and cache.get(_student_key(student_id, school), 0) >= ATTEMPTS


def _count(key):
    from core.ratelimit import count

    count(key, WINDOW)


def _failed(address, school, student_id=""):
    _count(_key(address, school))
    if (student_id or "").strip():
        _count(_student_key(student_id, school))


def lookup(school, student_id, code, address):
    """The student whose ID and code both match, or None. Every miss counts towards the limit."""
    from students.models import Student

    student = Student.objects.filter(school=school, student_id=(student_id or "").strip()).first()
    given = normalise(code)
    if student is None or not student.result_code or not given:
        _failed(address, school, student_id)
        return None
    if not secrets.compare_digest(normalise(student.result_code), given):
        _failed(address, school, student_id)
        return None
    return student


def published_results(student):
    """The current published results of a student, exam results first, newest first."""
    exams = (
        ResultSnapshot.objects.filter(
            enrollment__student=student, exam__status="published", version=F("exam__publication_version")
        )
        .select_related("exam__academic_year")
        .order_by("-exam__academic_year__start_date", "-exam__published_at")
    )
    combined = (
        CombinedSnapshot.objects.filter(
            enrollment__student=student, combined__status="published", version=F("combined__publication_version")
        )
        .select_related("combined__academic_year")
        .order_by("-combined__academic_year__start_date")
    )
    return list(exams), list(combined)
