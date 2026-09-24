"""
Phase 4c: tests and interviews, the merit list, offers and the waiting list.

The merit list comes out in the same order from the same results, the round's sibling rule
decides where a confirmed brother or sister stands, and offers never take more seats than the
class has.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from admissions import selection, services
from admissions.models import Application, AssessmentResult
from core.models import AuditLog
from tests.admissions_support import apply_online, family, login

NAMES = ["Anika", "Bashir", "Chaity", "Dipu", "Esha"]


@pytest.fixture
def sitting(erp, admissions):
    return selection.schedule(
        user=erp.staff,
        round_class=admissions.row,
        kind="test",
        starts_at=timezone.now() - timedelta(days=1),
        venue="Hall A",
        note="Bring two pencils.",
    )


def applicants(erp, admissions, count=4, paid=True):
    made = []
    for name in NAMES[:count]:
        application, _ = apply_online(admissions.row, first_name=name, guardian_phone=f"0171100{len(made):04d}")
        if paid:
            services.record_payment(user=erp.accountant, application=application, amount="500", method="cash")
        made.append(application)
    return made


def score(erp, sitting, application, value, came="yes"):
    result = AssessmentResult.objects.get(assessment=sitting, application=application)
    selection.record(user=erp.staff, assessment=sitting, rows={result.pk: (came, value, "")})


def sat(erp, admissions, sitting, scores):
    made = applicants(erp, admissions, len(scores))
    selection.book(user=erp.staff, assessment=sitting, applications=made)
    for application, value in zip(made, scores, strict=True):
        score(erp, sitting, application, value)
    return made


def names(rows):
    return [row.application.first_name for row in rows]


# ------------------------------------------------------------------ sittings


def test_only_the_classes_own_kind_of_assessment_can_be_scheduled(erp, admissions):
    with pytest.raises(ValidationError, match="admission test"):
        selection.schedule(
            user=erp.staff, round_class=admissions.row, kind="interview", starts_at=timezone.now(), venue="Room 1"
        )
    with pytest.raises(PermissionDenied):
        selection.schedule(
            user=erp.accountant, round_class=admissions.row, kind="test", starts_at=timezone.now(), venue="Hall"
        )


def test_a_sitting_is_named_in_the_schools_time(erp, admissions, sitting):
    from datetime import datetime

    sitting.starts_at = timezone.make_aware(datetime(2026, 11, 15, 10, 0))
    assert str(sitting) == "Admission test 15 Nov 2026 10:00"


def test_booking_asks_for_the_fee_first_and_keeps_to_the_capacity(erp, admissions, sitting):
    unpaid = applicants(erp, admissions, 1, paid=False)[0]
    booked, skipped = selection.book(user=erp.staff, assessment=sitting, applications=[unpaid])
    assert not booked and skipped[0][1] == "the application fee is not paid"
    sitting.capacity = 2
    sitting.save()
    paid = applicants(erp, admissions, 3)[:3]
    booked, skipped = selection.book(user=erp.staff, assessment=sitting, applications=paid)
    assert len(booked) == 2 and skipped[0][1] == "the sitting is full"
    booked[0].refresh_from_db()
    assert booked[0].status == "under_review"
    again, skipped = selection.book(user=erp.staff, assessment=sitting, applications=[booked[0]])
    assert not again and "already booked" in skipped[0][1]


def test_the_family_sees_the_booking_until_it_is_recorded(erp, admissions):
    upcoming = selection.schedule(
        user=erp.staff,
        round_class=admissions.row,
        kind="test",
        starts_at=timezone.now() + timedelta(days=2),
        venue="Hall A",
        note="Bring two pencils.",
    )
    application, raw = apply_online(admissions.row)
    services.record_payment(user=erp.accountant, application=application, amount="500", method="cash")
    selection.book(user=erp.staff, assessment=upcoming, applications=[application])
    client = family(erp.school, raw)
    page = client.get(f"/apply/{erp.school.slug}/my/").content.decode()
    assert "Hall A" in page and "Bring two pencils." in page and "Admission test" in page
    score(erp, upcoming, application, "70")
    assert "Hall A" not in client.get(f"/apply/{erp.school.slug}/my/").content.decode()


def test_scores_are_checked_before_anything_is_saved(erp, admissions, sitting):
    first, second = applicants(erp, admissions, 2)
    selection.book(user=erp.staff, assessment=sitting, applications=[first, second])
    results = {r.application_id: r.pk for r in sitting.results.all()}
    with pytest.raises(ValidationError, match="from 0 to 100"):
        selection.record(
            user=erp.staff,
            assessment=sitting,
            rows={results[first.pk]: ("yes", "70", ""), results[second.pk]: ("yes", "140", "")},
        )
    assert not AssessmentResult.objects.filter(score__isnull=False).exists()
    with pytest.raises(ValidationError, match="means the child came"):
        selection.record(user=erp.staff, assessment=sitting, rows={results[first.pk]: ("no", "70", "")})
    with pytest.raises(PermissionDenied):
        selection.record(user=erp.accountant, assessment=sitting, rows={})


# ------------------------------------------------------------------ the merit list


def test_the_list_is_ordered_by_pass_mark_score_and_then_who_applied_first(erp, admissions, sitting):
    made = sat(erp, admissions, sitting, ["62", "88", "35", "62"])
    ranked, waiting = selection.merit_list(admissions.row)
    # Bashir top; Anika and Dipu tie on 62, Anika applied first; Chaity is below the pass mark.
    assert names(ranked) == ["Bashir", "Anika", "Dipu", "Chaity"]
    assert [row.passes for row in ranked] == [True, True, True, False]
    assert not waiting
    assert selection.merit_list(admissions.row)[0][0].application == made[1]  # the same every time


@pytest.mark.parametrize(
    ("rule", "order"),
    [
        ("none", ["Bashir", "Anika", "Dipu", "Chaity"]),
        ("tie_break", ["Bashir", "Dipu", "Anika", "Chaity"]),
        ("priority", ["Dipu", "Bashir", "Anika", "Chaity"]),
    ],
)
def test_the_rounds_sibling_rule_places_a_confirmed_brother_or_sister(erp, admissions, sitting, rule, order):
    admissions.round.sibling_rule = rule
    admissions.round.save()
    made = sat(erp, admissions, sitting, ["62", "88", "35", "62"])
    dipu = made[3]
    Application.objects.filter(pk=dipu.pk).update(sibling_claimed=True, sibling_details="Ayesha")
    dipu.refresh_from_db()
    services.confirm_sibling(user=erp.staff, application=dipu, student=erp.student)
    # A sibling below the pass mark never jumps ahead of those who passed.
    chaity = made[2]
    Application.objects.filter(pk=chaity.pk).update(sibling=erp.student)
    assert names(selection.merit_list(admissions.row)[0]) == order


def test_an_absent_or_unrecorded_applicant_is_not_ranked(erp, admissions, sitting):
    first, second, third = applicants(erp, admissions, 3)
    selection.book(user=erp.staff, assessment=sitting, applications=[first, second])
    score(erp, sitting, first, "", came="no")
    ranked, waiting = selection.merit_list(admissions.row)
    assert not ranked
    assert [(row.application.first_name, row.absent) for row in waiting] == [
        ("Anika", True),
        ("Bashir", False),
        ("Chaity", False),
    ]


# ------------------------------------------------------------------ offers


def test_offers_follow_the_fixed_list_and_never_exceed_the_seats(erp, admissions, sitting):
    head = erp.admin
    sat(erp, admissions, sitting, ["62", "88", "35", "70"])
    with pytest.raises(PermissionDenied):
        selection.fix_merit_list(user=erp.staff, round_class=admissions.row)
    with pytest.raises(ValidationError, match="Fix the merit list"):
        selection.offer_places(user=head, round_class=admissions.row)
    selection.fix_merit_list(user=head, round_class=admissions.row)
    assert AuditLog.objects.filter(action="admissions.merit_fixed").exists()
    made = selection.offer_places(user=head, round_class=admissions.row)
    assert [a.first_name for a in made] == ["Bashir", "Dipu"]
    # Offers take children off the list; what is left is still the fixed list.
    assert selection.list_is_current(admissions.row, selection.merit_list(admissions.row)[0])
    with pytest.raises(ValidationError, match="Every seat"):
        selection.offer_places(user=head, round_class=admissions.row)
    waiting = selection.waitlist_rest(user=head, round_class=admissions.row)
    assert [a.first_name for a in waiting] == ["Anika"]
    turned = selection.turn_down_below_pass(user=head, round_class=admissions.row, reason="Below the pass mark")
    assert [a.first_name for a in turned] == ["Chaity"]
    assert Application.objects.get(first_name="Chaity").decision_reason == "Below the pass mark"
    # A seat comes free when an offer is declined; the waiting list moves up.
    bashir = Application.objects.get(first_name="Bashir")
    services.transition(bashir, "offer_declined")
    offered = selection.offer_next(user=head, round_class=admissions.row)
    assert offered.first_name == "Anika" and offered.status == "offered"
    with pytest.raises(ValidationError, match="Nobody is on the waiting list"):
        selection.offer_next(user=head, round_class=admissions.row)


def test_a_lapsed_offer_frees_its_seat_for_the_waiting_list(erp, admissions, sitting):
    head = erp.admin
    admissions.row.seats = 1
    admissions.row.save()
    sat(erp, admissions, sitting, ["80", "70"])
    selection.fix_merit_list(user=head, round_class=admissions.row)
    [first] = selection.offer_places(user=head, round_class=admissions.row)
    selection.waitlist_rest(user=head, round_class=admissions.row)
    with pytest.raises(ValidationError):
        selection.offer_next(user=head, round_class=admissions.row)
    Application.objects.filter(pk=first.pk).update(offer_expires_on=timezone.localdate() - timedelta(days=1))
    assert selection.offer_next(user=head, round_class=admissions.row).first_name == "Bashir"


def test_the_list_shows_when_results_changed_after_it_was_fixed(erp, admissions, sitting):
    made = sat(erp, admissions, sitting, ["60", "70"])
    selection.fix_merit_list(user=erp.admin, round_class=admissions.row)
    assert selection.list_is_current(admissions.row, selection.merit_list(admissions.row)[0])
    score(erp, sitting, made[0], "90")
    assert not selection.list_is_current(admissions.row, selection.merit_list(admissions.row)[0])
    page = login(erp.admin).get(f"/admissions/classes/{admissions.row.pk}/merit/").content.decode()
    assert "changed since the list was fixed" in page


def test_the_office_screens_run_a_sitting_and_the_list(erp, admissions):
    client = login(erp.staff)
    response = client.post(
        f"/admissions/classes/{admissions.row.pk}/assessments/",
        {"kind": "test", "starts_at": "2026-11-15T10:00", "venue": "Hall A", "capacity": "30"},
    )
    sitting = admissions.row.assessments.get()
    assert response["Location"] == f"/admissions/assessments/{sitting.pk}/"
    made = applicants(erp, admissions, 2)
    client.post(f"/admissions/assessments/{sitting.pk}/", {"action": "book", "application": [a.pk for a in made]})
    results = list(sitting.results.order_by("application__first_name"))
    assert len(results) == 2
    client.post(
        f"/admissions/assessments/{sitting.pk}/",
        {
            "action": "record",
            f"came_{results[0].pk}": "yes",
            f"score_{results[0].pk}": "71.5",
            f"came_{results[1].pk}": "no",
        },
    )
    results[0].refresh_from_db()
    assert results[0].score == Decimal("71.5") and results[0].recorded_by == erp.staff
    # The front office runs sittings; placing children is for the managers.
    client.post(f"/admissions/classes/{admissions.row.pk}/merit/", {"action": "fix"})
    assert not Application.objects.filter(merit_rank__isnull=False).exists()
    login(erp.admin).post(f"/admissions/classes/{admissions.row.pk}/merit/", {"action": "fix"})
    assert Application.objects.get(first_name="Anika").merit_rank == 1
