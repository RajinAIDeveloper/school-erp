"""Sample admissions for the demo school, so the module can be shown working from end to end."""

from datetime import date, timedelta
from decimal import Decimal

from django.utils import timezone

from . import selection, services
from .forms import PublicApplicationForm
from .models import AdmissionRound, RoundClass

APPLICANTS = [
    # first name, last name, born, guardian, phone, score (None: still to sit), paid, sibling claimed
    ("Nusrat", "Jahan", "2020-03-14", "Farhana Jahan", "01711200001", 86, True, False),
    ("Rafi", "Ahmed", "2020-06-15", "Nasrin Ahmed", "01711200002", 78, True, False),
    ("Tahmid", "Hasan", "2020-09-02", "Kamrul Hasan", "01711200003", 64, True, False),
    ("Mehjabin", "Rahman", "2020-01-20", "Shirin Rahman", "01711200004", 31, True, False),
    ("Samiul", "Islam", "2020-11-08", "Rokeya Islam", "01711200005", None, True, False),
    ("Ariana", "Chowdhury", "2020-05-27", "Tanvir Chowdhury", "01711200006", None, False, True),
]


def seed_admissions(*, school, level, admin):
    """
    Give the demo school online admissions and a round for next year's Class 1: a sitting that
    has been recorded, one still to come, a fixed merit list with offers, an accepted place, a
    waiting list and an application turned down. Does nothing if the school already has a round.
    """
    from academics.models import AcademicYear

    school.admissions_enabled = True
    school.save(update_fields=["admissions_enabled", "updated_at"])
    if AdmissionRound.objects.filter(school=school).exists():
        return 0
    today = timezone.localdate()
    now = timezone.now()
    year, _ = AcademicYear.objects.get_or_create(
        school=school, name="2027", defaults={"start_date": date(2027, 1, 1), "end_date": date(2027, 12, 31)}
    )
    admission_round = AdmissionRound.objects.create(
        school=school,
        academic_year=year,
        name="Admission 2027",
        opens_on=today - timedelta(days=14),
        closes_on=today + timedelta(days=30),
        application_fee=Decimal("500"),
        offer_days=7,
        is_published=True,
        instructions="Class 1 for the 2027 session. Children meet a teacher for a short, friendly interview.",
        instructions_bn="২০২৭ শিক্ষাবর্ষের প্রথম শ্রেণি। শিশুরা একজন শিক্ষকের সঙ্গে ছোট একটি সহজ সাক্ষাৎকারে অংশ নেবে।",
    )
    after, before = services.window_from_ages(date(2027, 1, 1), 6, 7)
    row = RoundClass.objects.create(
        school=school,
        admission_round=admission_round,
        class_level=level,
        seats=2,
        assessment=RoundClass.Assessment.INTERVIEW,
        max_score=100,
        pass_score=40,
        born_on_or_after=after,
        born_on_or_before=before,
        required_documents=["birth_certificate", "photo"],
    )
    done = selection.schedule(
        user=admin,
        round_class=row,
        kind="interview",
        starts_at=now - timedelta(days=3),
        venue="Room 101",
        note="Bring the application slip.",
    )
    coming = selection.schedule(
        user=admin,
        round_class=row,
        kind="interview",
        starts_at=now + timedelta(days=5),
        venue="Room 101",
        note="Bring the application slip.",
    )
    made = {}
    for first, last, born, guardian, phone, score, paid, sibling in APPLICANTS:
        data = {
            "first_name": first,
            "last_name": last,
            "gender": "F" if first in ("Nusrat", "Mehjabin", "Ariana") else "M",
            "date_of_birth": born,
            "guardian_name": guardian,
            "guardian_relation": "mother",
            "guardian_phone": phone,
            "address": "Dhanmondi, Dhaka",
            "heard_from": "family" if first in ("Nusrat", "Tahmid") else "website",
            "consent": "on",
        }
        if sibling:
            data.update({"sibling_claimed": "on", "sibling_details": "Brother in Class 3"})
        form = PublicApplicationForm(data)
        form.is_valid()
        application, _raw = services.submit(round_class=row, details=form.details())
        if paid:
            services.record_payment(user=admin, application=application, amount="500", method="cash")
        made[first] = (application, score)
    for application, score in made.values():
        if score is not None:
            selection.book(user=admin, assessment=done, applications=[application])
            result = done.results.get(application=application)
            selection.record(user=admin, assessment=done, rows={result.pk: ("yes", str(score), "")})
    selection.book(user=admin, assessment=coming, applications=[made["Samiul"][0]])
    selection.fix_merit_list(user=admin, round_class=row)
    selection.offer_places(user=admin, round_class=row)
    selection.waitlist_rest(user=admin, round_class=row)
    selection.turn_down_below_pass(
        user=admin, round_class=row, reason="Not ready for Class 1 yet; the school suggests applying next year."
    )
    services.transition(made["Nusrat"][0], "accepted")
    return len(made)
