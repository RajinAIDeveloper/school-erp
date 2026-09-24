"""
Assessments, the merit list, offers and the waiting list.

The merit list always comes out in the same order from the same results: those who reached
the pass mark first, then by score, with the round's rule for brothers and sisters, and then by
who applied first. It is worked out live for staff to look at. Offers go by the list once it
has been fixed, which writes each place on the application and in the audit log, so the order
offers were made in can always be shown.

Offers never take more seats than the class has: each one locks the class row while the seats
are counted. An offer holds its seat until it is accepted, declined or lapses.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import formats, timezone

from core.access import assert_actor_school, assert_school
from core.models import AuditLog

from . import services
from .models import AdmissionRound, Application, ApplicationEvent, Assessment, AssessmentResult, RoundClass

S = Application.Status
RANKABLE = [S.SUBMITTED, S.UNDER_REVIEW, S.WAITLISTED]
KINDS_NEEDED = {
    RoundClass.Assessment.NONE: set(),
    RoundClass.Assessment.TEST: {Assessment.Kind.TEST},
    RoundClass.Assessment.INTERVIEW: {Assessment.Kind.INTERVIEW},
    RoundClass.Assessment.BOTH: {Assessment.Kind.TEST, Assessment.Kind.INTERVIEW},
}


def _needs(user, permission, school):
    assert_actor_school(user, school)
    if not user.has_perm(permission):
        raise PermissionDenied


def _audit(school, user, action, obj, description):
    AuditLog.objects.create(
        school=school,
        user=user,
        action=action,
        model=obj._meta.label,
        object_id=str(obj.pk),
        description=description[:2000],
    )


# ------------------------------------------------------------------ sittings


@transaction.atomic
def schedule(*, user, round_class, kind, starts_at, venue, capacity=None, note=""):
    """A sitting of the test, or a round of interviews, that applicants can then be booked into."""
    _needs(user, "admissions.add_assessment", round_class.school)
    if kind not in KINDS_NEEDED[round_class.assessment]:
        raise ValidationError(
            f"{round_class.class_level} in this round has {round_class.get_assessment_display().lower()}."
        )
    venue = (venue or "").strip()
    if starts_at is None or not venue:
        raise ValidationError("Give the date, the time and the place.")
    if capacity is not None and capacity < 1:
        raise ValidationError("Give a capacity of at least one, or leave it empty.")
    sitting = Assessment.objects.create(
        school=round_class.school,
        round_class=round_class,
        kind=kind,
        starts_at=starts_at,
        venue=venue[:150],
        capacity=capacity,
        note=(note or "").strip()[:300],
    )
    _audit(round_class.school, user, "admissions.sitting_scheduled", sitting, f"{sitting} for {round_class}")
    return sitting


@transaction.atomic
def book(*, user, assessment, applications):
    """
    Book applicants into a sitting. Returns (booked, [(application, why not)]). Where the round
    asks for the fee first, an applicant who has not paid it is not booked.
    """
    _needs(user, "admissions.add_assessmentresult", assessment.school)
    sitting = (
        Assessment.objects.select_for_update().select_related("round_class__admission_round").get(pk=assessment.pk)
    )
    admission_round = sitting.round_class.admission_round
    taken = sitting.results.count()
    booked, skipped = [], []
    for application in applications:
        assert_school(sitting.school, application)
        application.refresh_from_db()
        if application.round_class_id != sitting.round_class_id:
            skipped.append((application, "applied for another class"))
        elif application.current_status not in RANKABLE:
            skipped.append((application, application.get_current_status_display().lower()))
        elif AssessmentResult.objects.filter(application=application, assessment__kind=sitting.kind).exists():
            skipped.append((application, f"already booked for {sitting.get_kind_display().lower()}"))
        elif admission_round.fee_before_assessment and not services.fee_state(application)["settled"]:
            skipped.append((application, "the application fee is not paid"))
        elif sitting.capacity is not None and taken >= sitting.capacity:
            skipped.append((application, "the sitting is full"))
        else:
            AssessmentResult.objects.create(school=sitting.school, assessment=sitting, application=application)
            taken += 1
            booked.append(application)
            when = formats.date_format(timezone.localtime(sitting.starts_at), "j M Y H:i")
            text = f"Booked for {sitting.get_kind_display().lower()} on {when}, {sitting.venue}."
            if application.status == S.SUBMITTED:
                services.transition(application, S.UNDER_REVIEW, user=user, reason=text)
            else:
                services._event(application, ApplicationEvent.Kind.NOTE, text, user)
    return booked, skipped


def _decimal(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValidationError(f"“{raw}” is not a score.") from None
    if not value.is_finite():
        raise ValidationError(f"“{raw}” is not a score.")
    return value


@transaction.atomic
def record(*, user, assessment, rows):
    """
    Record a sitting: `rows` maps each result's id to (came: "yes", "no" or "", score text,
    notes). A score means the applicant came. Nothing is saved if any row is wrong.
    """
    _needs(user, "admissions.change_assessmentresult", assessment.school)
    top = assessment.round_class.max_score
    results = {result.pk: result for result in assessment.results.select_for_update()}
    problems, changed = [], []
    for pk, (came, raw_score, notes) in rows.items():
        result = results.get(pk)
        if result is None:
            continue
        try:
            score = _decimal(raw_score)
        except ValidationError as error:
            problems.extend(error.messages)
            continue
        attended = {"yes": True, "no": False}.get(came)
        if score is not None:
            if attended is False:
                problems.append(f"{result.application.child_name}: a score means the child came.")
                continue
            attended = True
            if score < 0 or (top is not None and score > top):
                problems.append(f"{result.application.child_name}: give a score from 0 to {top}.")
                continue
        new = (attended, score, (notes or "").strip()[:300])
        if new != (result.attended, result.score, result.notes):
            result.attended, result.score, result.notes = new
            result.recorded_by = user
            result.recorded_at = timezone.now()
            changed.append(result)
    if problems:
        raise ValidationError(problems)
    for result in changed:
        result.save()
    if changed:
        _audit(
            assessment.school,
            user,
            "admissions.scores_recorded",
            assessment,
            f"{len(changed)} result(s) for {assessment}",
        )
    return changed


# ------------------------------------------------------------------ the merit list


@dataclass
class Row:
    application: Application
    score: Decimal | None
    complete: bool
    absent: bool
    passes: bool
    sibling: bool
    rank: int | None = None


def _row(application, round_class):
    needed = KINDS_NEEDED[round_class.assessment]
    results = list(application.assessment_results.all())
    absent = any(result.attended is False for result in results)
    total, complete = Decimal(0), True
    for kind in needed:
        scored = [r for r in results if r.assessment.kind == kind and r.attended and r.score is not None]
        if not scored:
            complete = False
            continue
        total += max(scored, key=lambda r: (r.recorded_at or r.created_at, r.pk)).score
    score = total if needed and complete else None
    bar = round_class.pass_score
    passes = complete and (bar is None or score is None or score >= bar)
    return Row(application, score, complete, absent, passes, application.sibling_id is not None)


def merit_list(round_class):
    """
    (ranked, not yet ranked). Ranked rows are those with every assessment the class needs
    scored, in merit order; the others are still waiting for a sitting, or missed one.
    """
    applications = (
        round_class.applications.filter(status__in=RANKABLE, purged_at__isnull=True)
        .select_related("sibling")
        .prefetch_related("assessment_results__assessment")
    )
    rows = [_row(application, round_class) for application in applications]
    rule = round_class.admission_round.sibling_rule

    def order(row):
        first = (row.application.submitted_at, row.application.pk)
        below = 0 if row.passes else 1
        score = -(row.score or 0)
        sibling = 0 if row.sibling else 1
        if rule == AdmissionRound.SiblingRule.PRIORITY:
            return (below, sibling, score, *first)
        if rule == AdmissionRound.SiblingRule.TIE_BREAK:
            return (below, score, sibling, *first)
        return (below, score, *first)

    ranked = sorted((row for row in rows if row.complete), key=order)
    for number, row in enumerate(ranked, start=1):
        row.rank = number
    waiting = sorted(
        (row for row in rows if not row.complete), key=lambda row: (row.application.submitted_at, row.application.pk)
    )
    return ranked, waiting


def list_is_current(round_class, ranked):
    """
    Whether the fixed list still stands: the same children in the same order with the same
    scores. Children offered a place have left the list, so the order is compared, not the numbers.
    """
    live = [row.application.pk for row in ranked]
    fixed = list(
        round_class.applications.filter(pk__in=live, merit_rank__isnull=False)
        .order_by("merit_rank")
        .values_list("pk", flat=True)
    )
    return fixed == live and all(row.application.score == row.score for row in ranked)


@transaction.atomic
def fix_merit_list(*, user, round_class):
    """Write each applicant's place and score, as the list stands now. Offers go by these places."""
    _needs(user, "admissions.decide_application", round_class.school)
    RoundClass.objects.select_for_update().get(pk=round_class.pk)
    ranked, waiting = merit_list(round_class)
    for row in ranked:
        Application.objects.filter(pk=row.application.pk).update(merit_rank=row.rank, score=row.score)
    Application.objects.filter(pk__in=[row.application.pk for row in waiting]).update(merit_rank=None, score=None)
    order = ", ".join(f"{row.rank}. {row.application.reference} ({row.score})" for row in ranked[:50])
    _audit(round_class.school, user, "admissions.merit_fixed", round_class, f"{round_class}: {order}")
    return ranked


def _fixed(round_class):
    """Applicants on the fixed list who reached the pass mark, in their places."""
    rows = round_class.applications.filter(status__in=RANKABLE, merit_rank__isnull=False).order_by("merit_rank")
    bar = round_class.pass_score
    return [a for a in rows if bar is None or a.score is None or a.score >= bar]


@transaction.atomic
def offer_places(*, user, round_class, how_many=None):
    """Offer the free seats to the top of the fixed list, in order. Returns the offers made."""
    _needs(user, "admissions.decide_application", round_class.school)
    locked = RoundClass.objects.select_for_update().get(pk=round_class.pk)
    free = locked.seats - services.seats_taken(locked)
    if how_many is not None:
        free = min(free, how_many)
    if free <= 0:
        raise ValidationError("Every seat is taken or held by an open offer.")
    candidates = _fixed(locked)
    if not candidates:
        raise ValidationError("Nobody on the fixed list is waiting for a place. Fix the merit list first.")
    made = []
    for application in candidates[:free]:
        made.append(
            services.transition(
                application, S.OFFERED, user=user, reason=f"Place {application.merit_rank} on the merit list."
            )
        )
    return made


@transaction.atomic
def waitlist_rest(*, user, round_class):
    """Everyone else on the fixed list who reached the pass mark goes on the waiting list, in order."""
    _needs(user, "admissions.decide_application", round_class.school)
    moved = []
    for application in _fixed(round_class):
        if application.status in (S.SUBMITTED, S.UNDER_REVIEW):
            moved.append(
                services.transition(
                    application, S.WAITLISTED, user=user, reason=f"Place {application.merit_rank} on the merit list."
                )
            )
    return moved


@transaction.atomic
def turn_down_below_pass(*, user, round_class, reason):
    """Applicants scored below the pass mark are not offered a place, with the reason given."""
    _needs(user, "admissions.decide_application", round_class.school)
    if round_class.pass_score is None:
        raise ValidationError("This class has no pass mark.")
    ranked, _waiting = merit_list(round_class)
    return [
        services.transition(row.application, S.NOT_OFFERED, user=user, reason=reason)
        for row in ranked
        if not row.passes
    ]


@transaction.atomic
def offer_next(*, user, round_class):
    """A seat has come free: offer it to the first on the waiting list."""
    _needs(user, "admissions.decide_application", round_class.school)
    RoundClass.objects.select_for_update().get(pk=round_class.pk)
    waiting = round_class.applications.filter(status=S.WAITLISTED).order_by("merit_rank", "submitted_at", "pk")
    application = waiting.first()
    if application is None:
        raise ValidationError("Nobody is on the waiting list.")
    return services.transition(application, S.OFFERED, user=user, reason="Offered from the waiting list.")
