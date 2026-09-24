"""
Event-driven SMS.

Every notification here is optional per school, queued rather than sent inline, and
deduplicated by a key describing the event. That matters: a school pays per message, a
guardian's phone is not a log file, and a retried request must not text a family twice.
Nothing in this module may raise into the caller — a messaging fault must never roll back
an attendance register or a fee receipt.
"""

import logging

from django.db import transaction

from .models import SMSMessage, SMSTemplate
from .services import normalize_bd_phone

log = logging.getLogger(__name__)

DEFAULT_TEMPLATES = {
    "absence": (
        "Absence alert",
        "Dear guardian, {student} of {class} was absent on {date}. - {school}",
    ),
    "fee_reminder": (
        "Fee reminder",
        "Dear guardian, {student} has {amount} outstanding, due {date}. - {school}",
    ),
    "payment_receipt": (
        "Payment received",
        "Received {amount} for {student}. Receipt {invoice}. Balance {balance}. - {school}",
    ),
    "result_published": (
        "Result published",
        "Results for {exam} are published. {student}: GPA {gpa}. - {school}",
    ),
    "admission_welcome": (
        "Admission welcome",
        "Welcome {student} to {school}. Student ID {invoice}, class {class}.",
    ),
    "homework_digest": (
        "Homework digest",
        "Dear guardian, {student} has {count} homework task(s) not handed in this week: {subjects}. - {school}",
    ),
}
# Messages that belong to a module: a school without it never gets their template.
MODULE_TEMPLATES = {"homework_digest": "homework"}


def ensure_default_templates(school):
    """Create the message bodies a school can then edit in its own words."""
    from core.modules import has_module

    created = []
    for key, (name, body) in DEFAULT_TEMPLATES.items():
        if key in MODULE_TEMPLATES and not has_module(school, MODULE_TEMPLATES[key]):
            continue
        template, was_new = SMSTemplate.objects.get_or_create(school=school, name=name, defaults={"body": body})
        if was_new:
            created.append(template)
    return created


def _template_body(school, key, fallback_name):
    template = SMSTemplate.objects.filter(school=school, name=fallback_name).first()
    if template:
        return template.body
    return DEFAULT_TEMPLATES[key][1]


def queue(school, *, key, phone, body, name="", dedupe_key="", redact_after_send=False):
    """
    Queue one message, ignoring duplicates for the same event.

    Returns the message, or None when it was already queued or the number is unusable.
    """
    try:
        number = normalize_bd_phone(phone)
    except Exception:  # noqa: BLE001 - a bad number on file must not break the caller
        log.info("Skipping %s notification: unusable number %r", key, phone)
        return None
    try:
        with transaction.atomic():
            if dedupe_key and SMSMessage.objects.filter(school=school, dedupe_key=dedupe_key).exists():
                return None
            return SMSMessage.objects.create(
                school=school,
                recipient_name=name,
                phone=number,
                body=body,
                dedupe_key=dedupe_key,
                redact_after_send=redact_after_send,
            )
    except Exception:  # noqa: BLE001 - including the unique constraint losing a race
        log.info("Skipping %s notification: already queued (%s)", key, dedupe_key)
        return None


def _render(body, **context):
    for placeholder, value in context.items():
        body = body.replace("{" + placeholder + "}", str(value))
    return body


def notify_absences(school, day, enrollments):
    """One message per absent student, to the guardian the school would ring."""
    if not school.notify_absence_sms or not enrollments:
        return 0
    body_template = _template_body(school, "absence", "Absence alert")
    queued = 0
    for enrollment in enrollments:
        student = enrollment.student
        guardian = student.primary_guardian
        if guardian is None or not getattr(guardian, "sms_opt_in", True):
            continue
        body = _render(
            body_template,
            student=student.full_name,
            **{"class": str(enrollment.section)},
            date=f"{day:%d %b %Y}",
            school=school.short_name or school.name,
            amount="",
            invoice="",
        )
        message = queue(
            school,
            key="absence",
            phone=guardian.phone,
            body=body,
            name=guardian.full_name,
            dedupe_key=f"absence:{enrollment.pk}:{day.isoformat()}",
        )
        queued += int(message is not None)
    return queued


def notify_payment(payment):
    school = payment.school
    if not school.notify_payment_sms:
        return None
    student = payment.invoice.student
    guardian = student.primary_guardian
    if guardian is None or not getattr(guardian, "sms_opt_in", True):
        return None
    symbol = school.currency_symbol
    body = _render(
        _template_body(school, "payment_receipt", "Payment received"),
        student=student.full_name,
        amount=f"{symbol}{payment.amount}",
        invoice=payment.receipt_no,
        balance=f"{symbol}{payment.invoice.balance}",
        school=school.short_name or school.name,
        date=f"{payment.date:%d %b %Y}",
    )
    return queue(
        school,
        key="payment",
        phone=guardian.phone,
        body=body,
        name=guardian.full_name,
        dedupe_key=f"payment:{payment.pk}",
    )


def notify_fee_due(school, invoices):
    """Reminders for outstanding invoices, at most one per invoice per day."""
    if not school.notify_due_sms:
        return 0
    from django.utils import timezone

    today = timezone.localdate()
    body_template = _template_body(school, "fee_reminder", "Fee reminder")
    symbol = school.currency_symbol
    queued = 0
    for invoice in invoices:
        guardian = invoice.student.primary_guardian
        if guardian is None or not getattr(guardian, "sms_opt_in", True):
            continue
        body = _render(
            body_template,
            student=invoice.student.full_name,
            amount=f"{symbol}{invoice.balance}",
            date=f"{invoice.due_date:%d %b %Y}",
            invoice=invoice.invoice_no,
            school=school.short_name or school.name,
            **{"class": str(invoice.enrollment.section)},
        )
        message = queue(
            school,
            key="fee_due",
            phone=guardian.phone,
            body=body,
            name=guardian.full_name,
            dedupe_key=f"fee_due:{invoice.pk}:{today.isoformat()}",
        )
        queued += int(message is not None)
    return queued


def notify_results_published(exam, snapshots):
    """One message per student for this publication version."""
    school = exam.school
    if not school.notify_results_sms:
        return 0
    body_template = _template_body(school, "result_published", "Result published")
    queued = 0
    for snapshot in snapshots:
        student = snapshot.enrollment.student
        guardian = student.primary_guardian
        if guardian is None or not getattr(guardian, "sms_opt_in", True):
            continue
        payload = snapshot.payload or {}
        body = _render(
            body_template,
            student=student.full_name,
            exam=exam.name,
            # Older templates say "GPA {gpa}"; a result without a GPA reads as its grades.
            gpa=payload.get("gpa") or payload.get("headline") or "-",
            result=payload.get("headline") or payload.get("result") or "",
            school=school.short_name or school.name,
            **{"class": payload.get("section", "")},
        )
        message = queue(
            school,
            key="results",
            phone=guardian.phone,
            body=body,
            name=guardian.full_name,
            dedupe_key=f"results:{exam.pk}:{snapshot.enrollment_id}:{snapshot.version}",
        )
        queued += int(message is not None)
    return queued


def notify_admission(student, enrollment):
    school = student.school
    if not school.notify_admission_sms:
        return None
    guardian = student.primary_guardian
    if guardian is None or not getattr(guardian, "sms_opt_in", True):
        return None
    body = _render(
        _template_body(school, "admission_welcome", "Admission welcome"),
        student=student.full_name,
        invoice=student.student_id,
        school=school.short_name or school.name,
        **{"class": str(enrollment.section) if enrollment else ""},
    )
    return queue(
        school,
        key="admission",
        phone=guardian.phone,
        body=body,
        name=guardian.full_name,
        dedupe_key=f"admission:{student.pk}",
    )


def notify_homework_digest(school, now=None):
    """
    Once a week, one message per child with homework not handed in over the last seven days,
    to the guardian the school would ring. Work checked in class counts only once the teacher
    has recorded it as not done: an exercise book nobody has looked at yet is not missing.
    Sending twice in one week sends nothing new.
    """
    from datetime import timedelta

    from django.db.models import F
    from django.db.models.functions import Coalesce
    from django.utils import timezone

    from core.modules import has_module
    from homework.followup import MISSING
    from homework.models import Submission, Task
    from students.models import Enrollment, Student

    if not (has_module(school, "homework") and school.notify_homework_sms):
        return 0
    now = now or timezone.now()
    rows = (
        Submission.objects.filter(
            school=school,
            task__status=Task.Status.PUBLISHED,
            enrollment__status=Enrollment.Status.ENROLLED,
            enrollment__student__status=Student.Status.ACTIVE,
        )
        .annotate(due=Coalesce(F("extended_to"), F("target__due_at")))
        .filter(due__gte=now - timedelta(days=7), due__lt=now)
        .filter(MISSING)
        .select_related("task__subject", "enrollment__student")
    )
    missing = {}
    for row in rows:
        entry = missing.setdefault(row.enrollment_id, {"enrollment": row.enrollment, "count": 0, "subjects": set()})
        entry["count"] += 1
        entry["subjects"].add(row.task.subject.name)
    if not missing:
        return 0
    body_template = _template_body(school, "homework_digest", "Homework digest")
    year, week, _day = timezone.localdate(now).isocalendar()
    queued = 0
    for entry in missing.values():
        student = entry["enrollment"].student
        guardian = student.primary_guardian
        if guardian is None or not getattr(guardian, "sms_opt_in", True):
            continue
        body = _render(
            body_template,
            student=student.full_name,
            count=entry["count"],
            subjects=", ".join(sorted(entry["subjects"])),
            school=school.short_name or school.name,
        )
        message = queue(
            school,
            key="homework",
            phone=guardian.phone,
            body=body,
            name=guardian.full_name,
            dedupe_key=f"homework_digest:{entry['enrollment'].pk}:{year}-W{week:02d}",
        )
        queued += int(message is not None)
    return queued
