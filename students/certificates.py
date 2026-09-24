"""
Transfer, leaving, character and studentship certificates: issued once, numbered, frozen and
verifiable.

These are the school's own documents. They are never an examination board's certificate, and
the printed page and the verification page both say so.
"""

import hashlib
import json
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school
from core.models import AuditLog, School

from .models import Certificate, Enrollment

PREFIX = {"transfer": "TC", "leaving": "LC", "character": "CC", "study": "SC"}
TITLES = {
    "transfer": ("Transfer Certificate", "ছাড়পত্র"),
    "leaving": ("School Leaving Certificate", "বিদ্যালয় ত্যাগের সনদপত্র"),
    "character": ("Character Certificate", "প্রশংসাপত্র"),
    # Proof that a child is studying here now: for a passport, visa, bank account or scholarship.
    "study": ("Studentship Certificate", "অধ্যয়ন প্রত্যয়নপত্র"),
}
CONDUCT = {
    "excellent": ("excellent", "চমৎকার"),
    "very_good": ("very good", "খুব ভালো"),
    "good": ("good", "ভালো"),
    "satisfactory": ("satisfactory", "সন্তোষজনক"),
}
BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
BN_MONTHS = [
    "জানুয়ারি",
    "ফেব্রুয়ারি",
    "মার্চ",
    "এপ্রিল",
    "মে",
    "জুন",
    "জুলাই",
    "আগস্ট",
    "সেপ্টেম্বর",
    "অক্টোবর",
    "নভেম্বর",
    "ডিসেম্বর",
]
REASON_LIMIT = 200
REMARKS_LIMIT = 300


def bn_number(value):
    return str(value).translate(BN_DIGITS)


def date_text(iso, language):
    from datetime import date

    if not iso:
        return ""
    day = date.fromisoformat(iso)
    if language == "bn":
        return bn_number(f"{day.day} {BN_MONTHS[day.month - 1]} {day.year}")
    return f"{day.day} {day:%B %Y}"


def fingerprint(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12].upper()
    return "-".join(digest[i : i + 4] for i in range(0, 12, 4))


def mask_name(name):
    """Initials and word lengths: enough to match a certificate, not enough to read it off."""
    return " ".join(word[0] + "•" * (len(word) - 1) for word in str(name).split() if word)


def _last_enrollment(student):
    return (
        Enrollment.objects.filter(student=student)
        .select_related("academic_year", "class_level", "section")
        .order_by("-academic_year__start_date", "-id")
        .first()
    )


def _next_serial(school, kind, year):
    prefix = f"{PREFIX[kind]}-{year}-"
    taken = Certificate.objects.filter(school=school, serial__startswith=prefix).values_list("serial", flat=True)
    highest = max((int(s.rsplit("-", 1)[1]) for s in taken if s.rsplit("-", 1)[1].isdigit()), default=0)
    return f"{prefix}{highest + 1:04d}"


def _check_details(school, student, kind, language, leaving_date, reason, conduct, remarks):
    if kind not in PREFIX:
        raise ValidationError("Choose a transfer, leaving, character or studentship certificate.")
    if language not in Certificate.Language.values:
        raise ValidationError("Choose English or Bangla.")
    if language == "bn" and not school.bangla_enabled:
        raise ValidationError("Bangla is switched off for this school. Switch it on in the school settings first.")
    if conduct not in CONDUCT:
        raise ValidationError("Choose how the student's conduct is described.")
    if len(reason) > REASON_LIMIT or len(remarks) > REMARKS_LIMIT:
        raise ValidationError(f"Keep the reason to {REASON_LIMIT} and the remarks to {REMARKS_LIMIT} characters.")
    if kind == "study" and student.status != student.Status.ACTIVE:
        raise ValidationError("A studentship certificate is only for a student who is studying here now.")
    if kind in ("transfer", "leaving"):
        if leaving_date is None:
            raise ValidationError("Give the date the student left.")
        if leaving_date > timezone.localdate():
            raise ValidationError("The leaving date cannot be in the future.")
        if leaving_date < student.admission_date:
            raise ValidationError("The leaving date is before the student's admission.")


def _outstanding(student):
    from fees.models import FeeInvoice

    return sum(
        (invoice.balance for invoice in FeeInvoice.objects.filter(student=student).exclude(status="cancelled")),
        start=0,
    )


def build_payload(school, student, *, kind, language, leaving_date, reason, conduct, remarks):
    enrollment = _last_enrollment(student)
    return {
        "kind": kind,
        "language": language,
        "student": student.full_name,
        "student_bn": student.name_bn,
        "student_first": student.first_name,
        "student_id": student.student_id,
        "father": student.father_name,
        "father_bn": student.father_name_bn,
        "mother": student.mother_name,
        "mother_bn": student.mother_name_bn,
        "date_of_birth": student.date_of_birth.isoformat() if student.date_of_birth else "",
        "admission_date": student.admission_date.isoformat() if student.admission_date else "",
        "still_enrolled": student.status == student.Status.ACTIVE,
        "class": enrollment.class_level.name if enrollment else "",
        "section": enrollment.section.name if enrollment and enrollment.section_id else "",
        "roll": enrollment.roll_number if enrollment else "",
        "session": str(enrollment.academic_year) if enrollment else "",
        "group": enrollment.get_group_display() if enrollment and enrollment.group else "",
        "board_registration_no": student.board_registration_no,
        "leaving_date": leaving_date.isoformat() if leaving_date else "",
        "reason": reason,
        "conduct": conduct,
        "remarks": remarks,
        "issued_on": timezone.localdate().isoformat(),
        "school": school.name,
        "school_address": school.address,
        "eiin": school.eiin,
        "head": school.principal_name,
    }


@transaction.atomic
def issue_certificate(
    *,
    school,
    user,
    student,
    kind,
    language="en",
    leaving_date=None,
    reason="",
    conduct="good",
    remarks="",
    force=False,
    replaces=None,
):
    """
    Issue a certificate, copying everything it prints so later edits to the record leave it
    unchanged. A transfer or leaving certificate waits until the student's fees are settled,
    unless someone who manages fees releases it.
    """
    assert_actor_school(user, school)
    assert_school(school, student)
    if not user.has_perm("students.add_certificate"):
        raise PermissionDenied
    reason, remarks = (reason or "").strip(), (remarks or "").strip()
    _check_details(school, student, kind, language, leaving_date, reason, conduct, remarks)
    if kind in ("transfer", "leaving"):
        owed = _outstanding(student)
        if owed > 0 and not force:
            raise ValidationError(
                f"{student} still owes {owed}. Settle the account first, or someone who manages fees may release it."
            )
        if owed > 0 and not user.has_perm("fees.change_feeinvoice"):
            raise PermissionDenied("Only staff who manage fees may release a certificate with an unpaid balance.")
    # One issuer at a time per school, so two certificates never take the same number.
    School.objects.select_for_update().get(pk=school.pk)
    payload = build_payload(
        school,
        student,
        kind=kind,
        language=language,
        leaving_date=leaving_date,
        reason=reason,
        conduct=conduct,
        remarks=remarks,
    )
    certificate = Certificate.objects.create(
        school=school,
        student=student,
        kind=kind,
        language=language,
        serial=_next_serial(school, kind, timezone.localdate().year),
        payload=payload,
        fingerprint=fingerprint(payload),
        verification_code=uuid.uuid4(),
        issued_by=user,
        replaces=replaces,
    )
    AuditLog.objects.create(
        school=school,
        user=user,
        action="certificate.issued",
        model=Certificate._meta.label,
        object_id=str(certificate.pk),
        description=f"{certificate.serial} {certificate.get_kind_display()} for {student}"
        + (f", replacing {replaces.serial}" if replaces else ""),
    )
    return certificate


@transaction.atomic
def revoke_certificate(*, school, user, certificate, reason):
    assert_actor_school(user, school)
    assert_school(school, certificate)
    if not user.has_perm("students.change_certificate"):
        raise PermissionDenied
    certificate = Certificate.objects.select_for_update().get(pk=certificate.pk)
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Say why the certificate is being revoked.")
    if certificate.is_revoked:
        raise ValidationError(f"{certificate.serial} is already revoked.")
    certificate.revoked_at = timezone.now()
    certificate.revoked_by = user
    certificate.revoke_reason = reason[:200]
    certificate.save(update_fields=["revoked_at", "revoked_by", "revoke_reason", "updated_at"])
    AuditLog.objects.create(
        school=school,
        user=user,
        action="certificate.revoked",
        model=Certificate._meta.label,
        object_id=str(certificate.pk),
        description=f"{certificate.serial}: {reason}",
    )
    return certificate


@transaction.atomic
def reissue_certificate(*, school, user, certificate, force=False):
    """
    Replace a certificate with a fresh one from the student's record as it stands now, with
    the same kind, language, dates and wording choices. The old one is revoked and points to
    the new one, so a holder of the old paper finds out it was replaced.
    """
    old = Certificate.objects.select_for_update().get(pk=certificate.pk)
    if hasattr(old, "replaced_by"):
        raise ValidationError(f"{old.serial} has already been replaced by {old.replaced_by.serial}.")
    if not user.has_perm("students.change_certificate"):
        raise PermissionDenied
    from datetime import date

    data = old.payload
    leaving = date.fromisoformat(data["leaving_date"]) if data.get("leaving_date") else None
    new = issue_certificate(
        school=school,
        user=user,
        student=old.student,
        kind=old.kind,
        language=old.language,
        leaving_date=leaving,
        reason=data.get("reason", ""),
        conduct=data.get("conduct", "good"),
        remarks=data.get("remarks", ""),
        force=force,
        replaces=old,
    )
    if not old.is_revoked:
        revoke_certificate(school=school, user=user, certificate=old, reason=f"Replaced by {new.serial}")
    return new


# ------------------------------------------------------------------ wording


def _parents_en(p):
    parents = [n for n in (p.get("father"), p.get("mother")) if n]
    return f", child of {' and '.join(parents)}" if parents else ""


def _parents_bn(p):
    parts = []
    if p.get("father_bn") or p.get("father"):
        parts.append(f"পিতা: {p.get('father_bn') or p.get('father')}")
    if p.get("mother_bn") or p.get("mother"):
        parts.append(f"মাতা: {p.get('mother_bn') or p.get('mother')}")
    return ", " + ", ".join(parts) if parts else ""


def _place_en(p):
    bits = [p["class"]] if p.get("class") else []
    if p.get("section"):
        bits.append(f"section {p['section']}")
    if p.get("roll"):
        bits.append(f"roll {p['roll']}")
    text = ", ".join(bits)
    if p.get("group"):
        text += f" ({p['group']} group)"
    if p.get("session"):
        text += f", in the {p['session']} session"
    return text


def _place_bn(p):
    bits = []
    if p.get("section"):
        bits.append(f"শাখা: {p['section']}")
    if p.get("roll"):
        bits.append(f"রোল: {bn_number(p['roll'])}")
    if p.get("session"):
        bits.append(f"শিক্ষাবর্ষ: {bn_number(p['session'])}")
    return f"{p.get('class', '')}" + (f" ({', '.join(bits)})" if bits else "")


def certificate_text(payload):
    """The certificate's title and paragraphs in its own language, from the frozen payload."""
    p, language, kind = payload, payload["language"], payload["kind"]
    title_en, title_bn = TITLES[kind]
    conduct_en, conduct_bn = CONDUCT.get(p.get("conduct"), CONDUCT["good"])
    born_en = f", born on {date_text(p['date_of_birth'], 'en')}" if p.get("date_of_birth") else ""
    born_bn = f", জন্ম তারিখ: {date_text(p['date_of_birth'], 'bn')}" if p.get("date_of_birth") else ""
    name_bn = p.get("student_bn") or p["student"]
    if language == "bn":
        if kind == "study":
            paragraphs = [
                f"এই মর্মে প্রত্যয়ন করা যাচ্ছে যে, {name_bn}{_parents_bn(p)}{born_bn}, বর্তমানে এই বিদ্যালয়ের "
                f"{_place_bn(p)}-এর একজন নিয়মিত শিক্ষার্থী।",
            ]
            if p.get("reason"):
                paragraphs.append(f"উদ্দেশ্য: {p['reason']}।")
            paragraphs.append(f"আমার জানামতে তার স্বভাব ও চরিত্র {conduct_bn}।")
            closing = "আমি তার উজ্জ্বল ভবিষ্যৎ কামনা করি।"
        elif kind == "character":
            status = "একজন নিয়মিত শিক্ষার্থী।" if p.get("still_enrolled") else "একজন নিয়মিত শিক্ষার্থী ছিল।"
            paragraphs = [
                f"এই মর্মে প্রত্যয়ন করা যাচ্ছে যে, {name_bn}{_parents_bn(p)}{born_bn}, এই বিদ্যালয়ের "
                f"{_place_bn(p)}-এর {status}",
                f"আমার জানামতে সে রাষ্ট্র বা শৃঙ্খলাবিরোধী কোনো কাজে জড়িত ছিল না। তার স্বভাব ও চরিত্র {conduct_bn}।",
            ]
            closing = "আমি তার উজ্জ্বল ভবিষ্যৎ কামনা করি।"
        else:
            paragraphs = [
                f"এই মর্মে প্রত্যয়ন করা যাচ্ছে যে, {name_bn}{_parents_bn(p)}{born_bn}, "
                f"{date_text(p['admission_date'], 'bn')} থেকে {date_text(p['leaving_date'], 'bn')} পর্যন্ত এই "
                f"বিদ্যালয়ের শিক্ষার্থী ছিল। বিদ্যালয় ত্যাগের সময় সে {_place_bn(p)}-এ অধ্যয়নরত ছিল।",
            ]
            if p.get("reason"):
                paragraphs.append(f"বিদ্যালয় ত্যাগের কারণ: {p['reason']}।")
            if p.get("board_registration_no"):
                paragraphs.append(f"বোর্ড রেজিস্ট্রেশন নম্বর: {bn_number(p['board_registration_no'])}।")
            paragraphs.append(f"বিদ্যালয়ে অবস্থানকালে তার আচরণ {conduct_bn} ছিল।")
            closing = "আমি তার সর্বাঙ্গীণ মঙ্গল কামনা করি।"
        if p.get("remarks"):
            paragraphs.append(f"মন্তব্য: {p['remarks']}")
        return title_bn, paragraphs, closing

    first = p.get("student_first") or p["student"]
    if kind == "study":
        paragraphs = [
            f"This is to certify that {p['student']}{_parents_en(p)}{born_en}, is a regular student of this school, "
            f"now studying in {_place_en(p)}.",
        ]
        if p.get("reason"):
            paragraphs.append(f"Issued for: {p['reason']}.")
        paragraphs.append(f"To the best of our knowledge, {first}'s conduct and character are {conduct_en}.")
        closing = f"We wish {first} every success."
    elif kind == "character":
        verb = "is" if p.get("still_enrolled") else "was"
        paragraphs = [
            f"This is to certify that {p['student']}{_parents_en(p)}{born_en}, {verb} a regular student of this "
            f"school in {_place_en(p)}.",
            f"To the best of our knowledge, {first} has not taken part in any activity against the state or "
            f"against discipline. {first}'s conduct and character are {conduct_en}.",
        ]
        closing = f"We wish {first} every success in life."
    else:
        paragraphs = [
            f"This is to certify that {p['student']}{_parents_en(p)}{born_en}, was a student of this school from "
            f"{date_text(p['admission_date'], 'en')} to {date_text(p['leaving_date'], 'en')}. At the time of "
            f"leaving, {first} was in {_place_en(p)}.",
        ]
        if p.get("reason"):
            paragraphs.append(f"Reason for leaving: {p['reason']}.")
        if p.get("board_registration_no"):
            paragraphs.append(f"Board registration number: {p['board_registration_no']}.")
        paragraphs.append(f"{first}'s conduct while at the school was {conduct_en}.")
        closing = f"We wish {first} every success."
    if p.get("remarks"):
        paragraphs.append(f"Remarks: {p['remarks']}")
    return title_en, paragraphs, closing
