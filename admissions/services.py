"""
The rules of an application, from the form to the office.

Every change of status goes through `transition`, which knows the moves allowed from each
status, who may make them, and writes each one on the timeline and in the audit log. An offer
past its expiry is lapsed whether or not anyone has looked; the first action on it afterwards
records the lapse, under a row lock, before anything else is done.

The family holds a private link. Only a hash of its token is stored, and reissuing the link
replaces the hash, so the old link stops working at once.
"""

import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import formats, timezone
from django.utils.translation import gettext

from core.access import assert_actor_school, assert_school
from core.models import AuditLog, School

from .consent import CONSENT_VERSION
from .models import Application, ApplicationDocument, ApplicationEvent, ApplicationPayment, RoundClass

S = Application.Status

ALLOWED = {
    S.SUBMITTED: {S.UNDER_REVIEW, S.NOT_OFFERED, S.WITHDRAWN},
    S.UNDER_REVIEW: {S.OFFERED, S.WAITLISTED, S.NOT_OFFERED, S.WITHDRAWN},
    S.WAITLISTED: {S.OFFERED, S.NOT_OFFERED, S.WITHDRAWN},
    S.OFFERED: {S.ACCEPTED, S.OFFER_DECLINED, S.WITHDRAWN, S.LAPSED},
    # A lapsed offer can be made again, or closed for good.
    S.LAPSED: {S.OFFERED, S.NOT_OFFERED},
    S.ACCEPTED: {S.ENROLLED, S.WITHDRAWN},
    # A decision can be looked at again.
    S.NOT_OFFERED: {S.UNDER_REVIEW},
    S.OFFER_DECLINED: set(),
    S.WITHDRAWN: set(),
    S.ENROLLED: set(),
}
# What a family may do from their own page.
FAMILY_MOVES = {S.WITHDRAWN, S.ACCEPTED, S.OFFER_DECLINED}
# Offering, wait-listing and turning down are decisions, for the school's managers.
DECISIONS = {S.OFFERED, S.WAITLISTED, S.NOT_OFFERED}
# Applications still in the running, for counts and duplicates.
LIVE = [S.SUBMITTED, S.UNDER_REVIEW, S.WAITLISTED, S.OFFERED, S.ACCEPTED]
MAX_DOCUMENTS = 12
REASON_LIMIT = 200


# ------------------------------------------------------------------ the private link


def hash_token(raw):
    return hashlib.sha256(str(raw).encode()).hexdigest()


def new_token():
    raw = secrets.token_urlsafe(16)
    return raw, hash_token(raw)


def by_token(raw):
    """The application a private link opens, or None. Cleared applications open nothing."""
    raw = str(raw or "")
    if not raw or len(raw) > 64:
        return None
    return (
        Application.objects.select_related("school", "round_class__admission_round", "round_class__class_level")
        .filter(token_hash=hash_token(raw), purged_at__isnull=True)
        .first()
    )


@transaction.atomic
def reissue_link(*, user, application):
    """A new private link, for a family who lost theirs. The old one stops working at once."""
    assert_actor_school(user, application.school)
    if not user.has_perm("admissions.change_application"):
        raise PermissionDenied
    locked = Application.objects.select_for_update().get(pk=application.pk)
    raw, digest = new_token()
    locked.token_hash = digest
    locked.token_generation += 1
    locked.save(update_fields=["token_hash", "token_generation", "updated_at"])
    _event(locked, ApplicationEvent.Kind.LINK, "A new private link was given; the old one no longer works.", user)
    _audit(locked, user, "application.link_reissued", f"{locked.reference}: link {locked.token_generation}")
    return raw


# ------------------------------------------------------------------ who may apply


def years_before(day, years):
    """The same day `years` earlier; 29 February becomes 28 February in a year without one."""
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def window_from_ages(on, youngest, oldest):
    """
    The birth dates of children aged `youngest` to `oldest` in whole years on the date `on`:
    (born on or after, born on or before).
    """
    if youngest < 0 or oldest < youngest:
        raise ValidationError("Give the youngest age first, and an oldest age no lower than it.")
    return years_before(on, oldest + 1) + timedelta(days=1), years_before(on, youngest)


def _day(value):
    return formats.date_format(value, "j M Y")


def birth_rule(round_class):
    """The date-of-birth rule in words, or "" when the class has none."""
    after, before = round_class.born_on_or_after, round_class.born_on_or_before
    if after and before:
        return gettext("born between %(start)s and %(end)s") % {"start": _day(after), "end": _day(before)}
    if after:
        return gettext("born on or after %(start)s") % {"start": _day(after)}
    if before:
        return gettext("born on or before %(end)s") % {"end": _day(before)}
    return ""


def age_problem(round_class, born):
    if round_class.admits_birth_date(born):
        return ""
    return gettext("For %(class)s in this round, children must be %(rule)s.") % {
        "class": round_class.class_level,
        "rule": birth_rule(round_class),
    }


# ------------------------------------------------------------------ applying


def _next_reference(school, year):
    prefix = f"APP-{year}-"
    taken = Application.objects.filter(school=school, reference__startswith=prefix).values_list("reference", flat=True)
    highest = max((int(r.rsplit("-", 1)[1]) for r in taken if r.rsplit("-", 1)[1].isdigit()), default=0)
    return f"{prefix}{highest + 1:04d}"


def find_duplicate(application):
    """
    An earlier application in the same round that looks like the same child. Only flagged for
    staff, never refused: if a birth registration number had to be unique, anyone who knew a
    child's number could stop that child applying by using it first.
    """
    others = (
        Application.objects.filter(
            school=application.school, round_class__admission_round=application.round_class.admission_round_id
        )
        .exclude(pk=application.pk)
        .filter(purged_at__isnull=True)
    )
    same_child = Q(
        date_of_birth=application.date_of_birth,
        first_name__iexact=application.first_name,
        guardian_phone=application.guardian_phone,
    )
    if application.birth_registration_no:
        same_child |= Q(birth_registration_no=application.birth_registration_no)
    return others.filter(same_child).order_by("submitted_at", "pk").first()


@transaction.atomic
def submit(*, round_class, details, user=None, ip=None, age_override_reason="", today=None):
    """
    Record an application: online by a family (no `user`), or at the office by staff. Returns
    (application, raw token); the raw token is shown to the family once and never stored.

    A date of birth outside the class's window is refused. At the office, staff may accept it
    with a reason, which stays on the application.
    """
    school = round_class.school
    today = today or timezone.localdate()
    office = user is not None
    if office:
        assert_actor_school(user, school)
        if not user.has_perm("admissions.add_application"):
            raise PermissionDenied
    elif not round_class.admission_round.is_open(today):
        raise ValidationError(gettext("Applications for this class are not open."))
    problem = age_problem(round_class, details["date_of_birth"])
    override = (age_override_reason or "").strip()[:REASON_LIMIT] if problem else ""
    if problem and not (office and override):
        raise ValidationError(problem)
    School.objects.select_for_update().get(pk=school.pk)
    raw, digest = new_token()
    application = Application(
        school=school,
        round_class=round_class,
        reference=_next_reference(school, round_class.admission_round.academic_year.start_date.year),
        token_hash=digest,
        channel=Application.Channel.OFFICE if office else Application.Channel.ONLINE,
        consent_version=CONSENT_VERSION,
        consent_at=timezone.now(),
        consent_ip=ip or None,
        age_override_reason=override,
        **details,
    )
    application.full_clean(exclude=["school"])
    application.save()
    duplicate = find_duplicate(application)
    if duplicate is not None:
        application.possible_duplicate_of = duplicate
        application.save(update_fields=["possible_duplicate_of", "updated_at"])
    how = "Entered at the office." if office else "Applied online."
    if override:
        how += f" Date of birth outside the class's window, accepted: {override}"
    _event(application, ApplicationEvent.Kind.STATUS, how, user, to_status=S.SUBMITTED)
    _audit(application, user, "application.submitted", f"{application.reference} for {round_class}")
    return application, raw


@transaction.atomic
def correct_details(*, user, application, details):
    """Staff put right what a family mistyped. The timeline says which fields changed."""
    assert_actor_school(user, application.school)
    if not user.has_perm("admissions.change_application"):
        raise PermissionDenied
    locked = Application.objects.select_for_update().get(pk=application.pk)
    changed = [name for name, value in details.items() if getattr(locked, name) != value]
    if not changed:
        return locked
    for name in changed:
        setattr(locked, name, details[name])
    locked.full_clean(exclude=["school"])
    locked.save()
    _event(locked, ApplicationEvent.Kind.DETAILS, "Corrected: " + ", ".join(changed).replace("_", " "), user)
    _audit(locked, user, "application.corrected", f"{locked.reference}: {', '.join(changed)}")
    return locked


# ------------------------------------------------------------------ statuses


def seats_taken(round_class, *, today=None):
    """Places accepted or enrolled, and offers still open: an offer holds its seat until it lapses."""
    today = today or timezone.localdate()
    return (
        Application.objects.filter(round_class=round_class)
        .filter(Q(status__in=[S.ACCEPTED, S.ENROLLED]) | Q(status=S.OFFERED, offer_expires_on__gte=today))
        .count()
    )


def _may_move(user, to):
    if user is None:
        return to in FAMILY_MOVES
    if to in DECISIONS:
        return user.has_perm("admissions.decide_application")
    return user.has_perm("admissions.change_application")


@transaction.atomic
def transition(application, to, *, user=None, reason="", today=None):
    """
    Move an application to another status, if that move is allowed and this person may make it.
    `user` None is the family, from their private page.
    """
    today = today or timezone.localdate()
    if user is not None:
        assert_actor_school(user, application.school)
    locked = (
        Application.objects.select_for_update().select_related("round_class__admission_round").get(pk=application.pk)
    )
    if locked.purged_at is not None:
        raise ValidationError(gettext("This application has been cleared."))
    if locked.status == S.OFFERED and locked.offer_expires_on and locked.offer_expires_on < today:
        _move(locked, S.LAPSED, None, f"The offer lapsed after {_day(locked.offer_expires_on)}.")
        if to == S.LAPSED:
            return locked
    reason = (reason or "").strip()
    if to not in ALLOWED.get(locked.status, set()):
        if locked.status == S.LAPSED and to in (S.ACCEPTED, S.OFFER_DECLINED):
            raise ValidationError(
                gettext("This offer lapsed after %(date)s. Contact the school office.")
                % {"date": _day(locked.offer_expires_on)}
            )
        raise ValidationError(
            gettext("An application that is %(status)s cannot be moved to %(to)s.")
            % {"status": locked.get_status_display().lower(), "to": S(to).label.lower()}
        )
    if not _may_move(user, to):
        raise PermissionDenied
    if to == S.NOT_OFFERED and not reason:
        raise ValidationError("Give the reason the child is not offered a place; it stays on the application.")
    if len(reason) > 300:
        raise ValidationError("Keep the reason to 300 characters.")
    if to == S.OFFERED:
        # The class row is locked while seats are counted, so two offers at once cannot both
        # take the last seat.
        round_class = RoundClass.objects.select_for_update().get(pk=locked.round_class_id)
        if seats_taken(round_class, today=today) >= round_class.seats:
            raise ValidationError(f"Every seat in {round_class.class_level} is taken or held by an open offer.")
        locked.offer_expires_on = today + timedelta(days=round_class.admission_round.offer_days)
    if to == S.ACCEPTED:
        locked.accepted_at = timezone.now()
    if to in DECISIONS:
        locked.decision_reason = reason
    text = reason or ("By the family." if user is None else "")
    if to == S.OFFERED:
        text = (text + " " if text else "") + f"Open until {_day(locked.offer_expires_on)}."
    _move(locked, to, user, text)
    return locked


def _move(application, to, user, text):
    was = application.status
    application.status = to
    application.save()
    _event(application, ApplicationEvent.Kind.STATUS, text, user, from_status=was, to_status=to)
    _audit(application, user, f"application.{to}", f"{application.reference}: {was} to {to}. {text}".strip())


# ------------------------------------------------------------------ documents


def add_document(*, application, kind, upload, user=None):
    """
    A document for the application: from the family's page (`user` None) or scanned at the
    office. Checked by what is in it, a PDF or a photo, and stored under a random name.
    """
    from core.files import prepare

    from .models import DOCUMENT_LABELS

    if user is not None:
        assert_actor_school(user, application.school)
        if not user.has_perm("admissions.add_applicationdocument"):
            raise PermissionDenied
    if kind not in DOCUMENT_LABELS:
        raise ValidationError(gettext("Choose which document this is."))
    if application.status not in LIVE:
        raise ValidationError(gettext("Documents can no longer be added to this application."))
    if application.documents.count() >= MAX_DOCUMENTS:
        raise ValidationError(gettext("An application can have at most 12 documents."))
    content, file_kind = prepare(upload, word=False)
    document = ApplicationDocument(
        school=application.school,
        application=application,
        kind=kind,
        file_kind=file_kind,
        size=content.size,
        uploaded_by=user,
    )
    document.file.save(content.name, content, save=False)
    document.save()
    who = "the family" if user is None else "the office"
    _event(application, ApplicationEvent.Kind.DOCUMENT, f"{document.get_kind_display()} added by {who}.", user)
    return document


@transaction.atomic
def check_document(*, user, document, accept, reason=""):
    assert_actor_school(user, document.school)
    if not user.has_perm("admissions.change_applicationdocument"):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not accept and not reason:
        raise ValidationError("Say why the document is not accepted; the family sees it.")
    if len(reason) > REASON_LIMIT:
        raise ValidationError(f"Keep the reason to {REASON_LIMIT} characters.")
    document.status = ApplicationDocument.Status.ACCEPTED if accept else ApplicationDocument.Status.REJECTED
    document.rejection_reason = "" if accept else reason
    document.checked_by = user
    document.checked_at = timezone.now()
    document.save()
    verdict = "accepted" if accept else f"not accepted: {reason}"
    _event(document.application, ApplicationEvent.Kind.DOCUMENT, f"{document.get_kind_display()} {verdict}", user)
    return document


def documents_missing(application):
    """Required documents with nothing accepted or waiting to be checked: (kind, label) pairs."""
    from .models import DOCUMENT_LABELS

    held = set(application.documents.exclude(status=ApplicationDocument.Status.REJECTED).values_list("kind", flat=True))
    return [
        (kind, DOCUMENT_LABELS[kind])
        for kind in application.round_class.required_documents
        if kind in DOCUMENT_LABELS and kind not in held
    ]


# ------------------------------------------------------------------ siblings and notes


@transaction.atomic
def confirm_sibling(*, user, application, student):
    """
    Staff confirm the brother or sister a family named. The public form never looks this up,
    so applying reveals nothing about who studies at the school.
    """
    assert_actor_school(user, application.school)
    if not user.has_perm("admissions.change_application"):
        raise PermissionDenied
    if student is not None:
        assert_school(application.school, student)
        if student.status != student.Status.ACTIVE:
            raise ValidationError(f"{student} is not studying at the school now.")
    application.sibling = student
    application.sibling_verified_by = user if student else None
    application.sibling_verified_at = timezone.now() if student else None
    application.save(update_fields=["sibling", "sibling_verified_by", "sibling_verified_at", "updated_at"])
    text = f"Sibling confirmed: {student}." if student else "Sibling cleared."
    _event(application, ApplicationEvent.Kind.SIBLING, text, user)
    _audit(application, user, "application.sibling", f"{application.reference}: {text}")
    return application


def add_note(*, user, application, text):
    assert_actor_school(user, application.school)
    if not user.has_perm("admissions.change_application"):
        raise PermissionDenied
    text = (text or "").strip()
    if not text:
        raise ValidationError("Write the note first.")
    if len(text) > 500:
        raise ValidationError("Keep a note to 500 characters.")
    return _event(application, ApplicationEvent.Kind.NOTE, text, user)


# ------------------------------------------------------------------ the application fee


def fee_state(application):
    """What the family owes, has paid and still owes; waived when the school has let it go."""
    due = application.round_class.admission_round.application_fee
    paid = application.payments.filter(voided_at__isnull=True).aggregate(total=Sum("amount"))["total"] or Decimal(0)
    waived = bool(application.fee_waived_reason)
    outstanding = Decimal(0) if waived else max(due - paid, Decimal(0))
    return {
        "due": due,
        "paid": paid,
        "waived": waived,
        "outstanding": outstanding,
        "settled": due <= 0 or waived or outstanding <= 0,
    }


def _next_receipt(school, year):
    prefix = f"APF-{year}-"
    taken = ApplicationPayment.objects.filter(school=school, receipt_no__startswith=prefix).values_list(
        "receipt_no", flat=True
    )
    highest = max((int(r.rsplit("-", 1)[1]) for r in taken if r.rsplit("-", 1)[1].isdigit()), default=0)
    return f"{prefix}{highest + 1:04d}"


@transaction.atomic
def record_payment(*, user, application, amount, method, reference="", date=None):
    """
    The application fee, paid at the office: a receipt, and Dr cash, bank or mobile money /
    Cr Admission Fee Income in the ledger. The ledger's narration names the receipt and the
    application, never the child, so it holds nothing personal once the application is cleared.
    """
    from finance.models import Account, JournalEntry, ensure_default_accounts, record_simple_entry

    school = application.school
    assert_actor_school(user, school)
    if not user.has_perm("admissions.add_applicationpayment"):
        raise PermissionDenied
    date = date or timezone.localdate()
    if date > timezone.localdate():
        raise ValidationError("A payment cannot be dated in the future.")
    try:
        amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValidationError("Give the amount paid.") from None
    reference = (reference or "").strip()
    if method not in ApplicationPayment.Method.values:
        raise ValidationError("Choose how the fee was paid.")
    if method != "cash" and not reference:
        raise ValidationError("Give the transaction ID or cheque number.")
    School.objects.select_for_update().get(pk=school.pk)
    locked = Application.objects.select_for_update().get(pk=application.pk)
    state = fee_state(locked)
    if state["waived"]:
        raise ValidationError("The fee for this application has been waived.")
    if not amount.is_finite() or amount <= 0 or amount > state["outstanding"]:
        raise ValidationError(f"Give an amount above zero and no more than the {state['outstanding']} still owed.")
    ensure_default_accounts(school)
    receipt_no = _next_receipt(school, date.year)
    entry = record_simple_entry(
        school,
        date,
        f"Application fee {receipt_no} for {locked.reference}",
        Account.objects.get(school=school, code=ApplicationPayment.METHOD_ACCOUNT_CODE[method]),
        Account.objects.get(school=school, code="4020"),
        amount,
        source=JournalEntry.Source.ADMISSION,
        reference=receipt_no,
        user=user,
    )
    payment = ApplicationPayment.objects.create(
        school=school,
        application=locked,
        receipt_no=receipt_no,
        amount=amount,
        method=method,
        reference=reference,
        date=date,
        received_by=user,
        journal_entry=entry,
    )
    _event(locked, ApplicationEvent.Kind.PAYMENT, f"Fee {amount} received, receipt {receipt_no}.", user)
    _audit(locked, user, "application.fee_paid", f"{receipt_no}: {amount} for {locked.reference}")
    return payment


@transaction.atomic
def void_payment(*, user, payment, reason):
    """A receipt written in error: the ledger entry is reversed, and the receipt kept, marked void."""
    from finance.models import assert_period_open

    school = payment.school
    assert_actor_school(user, school)
    if not user.has_perm("admissions.change_applicationpayment"):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give the reason the receipt is void; it goes on the record.")
    if len(reason) > REASON_LIMIT:
        raise ValidationError(f"Keep the reason to {REASON_LIMIT} characters.")
    School.objects.select_for_update().get(pk=school.pk)
    locked = ApplicationPayment.objects.select_for_update().select_related("journal_entry").get(pk=payment.pk)
    if locked.is_voided:
        raise ValidationError("This receipt is already void.")
    # Checked before anything is written, so a refused void leaves the receipt as it was.
    assert_period_open(school, locked.date)
    assert_period_open(school, timezone.localdate())
    locked.voided_at = timezone.now()
    locked.voided_by = user
    locked.void_reason = reason
    locked.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_at"])
    if locked.journal_entry_id and locked.journal_entry.status == "posted":
        locked.journal_entry.reverse(user=user, narration=f"Void application fee {locked.receipt_no}: {reason}")
    _event(locked.application, ApplicationEvent.Kind.PAYMENT, f"Receipt {locked.receipt_no} void: {reason}", user)
    _audit(locked.application, user, "application.fee_voided", f"{locked.receipt_no}: {reason}")
    return locked


@transaction.atomic
def waive_fee(*, user, application, reason):
    assert_actor_school(user, application.school)
    if not user.has_perm("admissions.change_applicationpayment"):
        raise PermissionDenied
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("Give the reason the fee is waived; it goes on the record.")
    if len(reason) > REASON_LIMIT:
        raise ValidationError(f"Keep the reason to {REASON_LIMIT} characters.")
    locked = Application.objects.select_for_update().get(pk=application.pk)
    if locked.fee_waived_reason:
        raise ValidationError("The fee is already waived.")
    if fee_state(locked)["paid"] > 0:
        raise ValidationError("Part of the fee has been paid. Void the receipt first if it should not stand.")
    locked.fee_waived_reason = reason
    locked.fee_waived_by = user
    locked.fee_waived_at = timezone.now()
    locked.save(update_fields=["fee_waived_reason", "fee_waived_by", "fee_waived_at", "updated_at"])
    _event(locked, ApplicationEvent.Kind.PAYMENT, f"Fee waived: {reason}", user)
    _audit(locked, user, "application.fee_waived", f"{locked.reference}: {reason}")
    return locked


# ------------------------------------------------------------------ the record


def _event(application, kind, text, user, *, from_status="", to_status=""):
    return ApplicationEvent.objects.create(
        school=application.school,
        application=application,
        kind=kind,
        text=(text or "")[:500],
        by=user,
        from_status=from_status,
        to_status=to_status,
    )


def _audit(application, user, action, description):
    AuditLog.objects.create(
        school=application.school,
        user=user,
        action=action,
        model=application._meta.label,
        object_id=str(application.pk),
        description=description,
    )
