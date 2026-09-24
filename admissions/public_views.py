"""
The pages a family uses without an account: the school's open rounds, the form, and their own page.

Every page answers "not found" unless the school is active and has the admissions module.
None may be cached or indexed, and none sends its address to another site. (Not "no-referrer":
a browser then sends a form's origin as "null", and the CSRF check turns the family away.)

The private link is opened once, at /apply/<school>/t/<token>/, which keeps the application in this
browser's session and moves on to the family's page, so the token leaves the address bar and
the history. The session also keeps the raw link while it is needed for the printable slip.
"""

import secrets
import time
from datetime import timedelta

from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.translation import gettext

from core import ratelimit
from core.access import public
from core.files import too_large
from core.language import public_language, switch_language
from core.modules import has_module
from core.security import client_ip

from . import services
from .forms import PublicApplicationForm
from .models import DOCUMENT_LABELS, AdmissionRound, Application, RoundClass
from .slip import slip_pdf

S = Application.Status
SESSION = "admission_links"  # {application pk: raw token}
DONE = "admission_done"
FORM_SALT = "admissions.form"
FASTEST = 5  # seconds: a person takes longer than this to fill the form in
FORM_WINDOW = 3 * 3600
# Form posts from one address in an hour. Generous: mobile networks put many families behind
# one address, and the school-wide ceiling below is what stops a flood.
TRIES = 60
SCHOOL_HOURLY = 300  # applications to one school in an hour
LINK_TRIES = 30  # links that open nothing, from one address in 15 minutes
WITHDRAWABLE = {S.SUBMITTED, S.UNDER_REVIEW, S.WAITLISTED, S.OFFERED, S.ACCEPTED}


def _school(slug):
    from core.models import School

    school = get_object_or_404(School, slug=slug, is_active=True)
    if not has_module(school, "admissions"):
        raise Http404
    return school


def _private(response):
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "same-origin"
    return response


def _render(request, school, template, context=None, status=200):
    context = {"school": school, "bangla": school is not None and school.bangla_enabled, **(context or {})}
    return _private(render(request, template, context, status=status))


def _enter(request, slug):
    """The school, in the visitor's language; or a redirect that records a change of language."""
    school = _school(slug)
    switched = switch_language(request)
    if switched is not None:
        return school, _private(switched)
    public_language(request, school)
    return school, None


def _links(request):
    held = request.session.get(SESSION, {})
    return held if isinstance(held, dict) else {}


def _hold(request, application, raw):
    held = dict(_links(request))
    held[str(application.pk)] = raw
    request.session[SESSION] = held


def _mine(request, school):
    """The applications at this school that this browser holds a current link for."""
    held = _links(request)
    ids = [int(key) for key in held if str(key).isdigit()]
    found, stale = [], []
    rows = Application.objects.filter(school=school, pk__in=ids, purged_at__isnull=True).select_related(
        "round_class__admission_round__academic_year", "round_class__class_level"
    )
    for application in rows:
        if secrets.compare_digest(application.token_hash, services.hash_token(held[str(application.pk)])):
            found.append(application)
        else:
            stale.append(str(application.pk))
    if stale:
        # The office gave this family a new link: the old one no longer opens anything.
        request.session[SESSION] = {key: value for key, value in held.items() if key not in stale}
    return found


def _link(request, application, raw):
    return request.build_absolute_uri(reverse("apply:open", args=[application.school.slug, raw]))


# ------------------------------------------------------------------ the school's rounds


@public
def landing(request, slug):
    school, switched = _enter(request, slug)
    if switched:
        return switched
    today = timezone.localdate()
    rounds = (
        AdmissionRound.objects.filter(school=school, is_published=True, closes_on__gte=today)
        .select_related("academic_year")
        .prefetch_related("classes__class_level")
        .order_by("opens_on", "id")
    )
    shown, upcoming = [], []
    for admission_round in rounds:
        classes = [
            {
                "row": row,
                "rule": services.birth_rule(row),
                "left": max(row.seats - services.seats_taken(row, today=today), 0),
            }
            for row in admission_round.classes.all()
        ]
        instructions = admission_round.instructions
        if request.LANGUAGE_CODE == "bn" and admission_round.instructions_bn:
            instructions = admission_round.instructions_bn
        item = {"round": admission_round, "classes": classes, "instructions": instructions}
        (shown if admission_round.opens_on <= today else upcoming).append(item)
    return _render(
        request,
        school,
        "admissions/public/landing.html",
        {"rounds": shown, "upcoming": upcoming, "held": bool(_mine(request, school))},
    )


# ------------------------------------------------------------------ the form


def _started(request):
    """The form's signed start time, kept across a refused send so a person is not held back twice."""
    posted = request.POST.get("started", "")
    try:
        signing.loads(posted, salt=FORM_SALT, max_age=FORM_WINDOW)
        return posted
    except signing.BadSignature:
        return signing.dumps(time.time(), salt=FORM_SALT)


def _trap(request):
    """Why a send looks like a machine's rather than a person's, or ""."""
    if request.POST.get("hp_confirm"):
        return gettext("The form could not be sent. Please try again.")
    try:
        started = signing.loads(request.POST.get("started", ""), salt=FORM_SALT, max_age=FORM_WINDOW)
    except signing.BadSignature:
        return gettext("The form was open too long. Check the details and send it again.")
    if time.time() - float(started) < FASTEST:
        return gettext("The form could not be sent. Please try again.")
    return ""


@public
def apply(request, slug, pk):
    school, switched = _enter(request, slug)
    if switched:
        return switched
    round_class = get_object_or_404(
        RoundClass.objects.select_related("admission_round__academic_year", "class_level"), pk=pk, school=school
    )
    if not round_class.admission_round.is_open():
        messages.info(request, gettext("Applications for this class are not open."))
        return _private(redirect("apply:landing", slug=school.slug))
    address = client_ip(request)
    error = ""
    form = PublicApplicationForm(request.POST or None)
    if request.method == "POST":
        from_here = ratelimit.key("apply-address", school.pk, address)
        to_school = ratelimit.key("apply-school", school.pk)
        if ratelimit.reached(from_here, TRIES) or ratelimit.reached(to_school, SCHOOL_HOURLY):
            error = gettext(
                "Too many applications have been sent from here. Try again in an hour, or apply at the school office."
            )
        else:
            ratelimit.count(from_here, 3600)
            error = _trap(request)
            if not error and form.is_valid():
                problem = services.age_problem(round_class, form.cleaned_data["date_of_birth"])
                if problem:
                    form.add_error("date_of_birth", problem)
                else:
                    try:
                        application, raw = services.submit(
                            round_class=round_class, details=form.details(), ip=address or None
                        )
                    except ValidationError as refused:
                        form.add_error(None, refused)
                    else:
                        ratelimit.count(to_school, 3600)
                        request.session.cycle_key()
                        _hold(request, application, raw)
                        request.session[DONE] = application.pk
                        return _private(redirect("apply:done", slug=school.slug))
    return _render(
        request,
        school,
        "admissions/public/form.html",
        {
            "form": form,
            "round_class": round_class,
            "rule": services.birth_rule(round_class),
            "started": _started(request) if request.method == "POST" else signing.dumps(time.time(), salt=FORM_SALT),
            "error": error,
        },
    )


@public
def done(request, slug):
    school, switched = _enter(request, slug)
    if switched:
        return switched
    wanted = request.session.get(DONE)
    application = next((a for a in _mine(request, school) if a.pk == wanted), None)
    if application is None:
        return _private(redirect("apply:mine", slug=school.slug))
    return _render(
        request,
        school,
        "admissions/public/done.html",
        {"application": application, "link": _link(request, application, _links(request)[str(application.pk)])},
    )


# ------------------------------------------------------------------ the private link


@public
def open_link(request, slug, token):
    school, switched = _enter(request, slug)
    if switched:
        return switched
    misses = ratelimit.key("apply-link", client_ip(request))
    if ratelimit.reached(misses, LINK_TRIES):
        return _render(request, school, "admissions/public/link_invalid.html", {"busy": True}, status=429)
    application = services.by_token(token)
    if application is None or application.school_id != school.pk:
        ratelimit.count(misses, 15 * 60)
        return _render(request, school, "admissions/public/link_invalid.html", status=404)
    request.session.cycle_key()
    _hold(request, application, token)
    return _private(redirect("apply:mine", slug=school.slug))


NEXT_STEPS = {
    S.SUBMITTED: lambda a: gettext("The school has your application and will look at it soon."),
    S.UNDER_REVIEW: lambda a: gettext("The school is looking at your application."),
    S.OFFERED: lambda a: (
        gettext("Your child is offered a place. Accept or decline it by %(date)s.")
        % {"date": formats.date_format(a.offer_expires_on, "j M Y")}
    ),
    S.WAITLISTED: lambda a: gettext(
        "Your child is on the waiting list. If a place comes free, the school offers it here."
    ),
    S.NOT_OFFERED: lambda a: gettext(
        "The school cannot offer your child a place this time. For more, contact the school office."
    ),
    S.LAPSED: lambda a: gettext("The offer was not answered in time and has lapsed. Contact the school office."),
    S.ACCEPTED: lambda a: gettext("You have accepted the place. The school will tell you about joining."),
    S.OFFER_DECLINED: lambda a: gettext("You declined the place."),
    S.WITHDRAWN: lambda a: gettext("The application was withdrawn."),
    S.ENROLLED: lambda a: gettext("Your child has joined the school. Welcome!"),
}


def _card(application):
    status = application.current_status
    live = status in services.LIVE
    kinds = [kind for kind in application.round_class.required_documents if kind in DOCUMENT_LABELS] + ["other"]
    missing = services.documents_missing(application)
    return {
        "application": application,
        "status": status,
        "status_label": application.get_current_status_display(),
        "next": NEXT_STEPS[status](application),
        "documents": list(application.documents.all()),
        "missing": missing,
        "kinds": [(kind, DOCUMENT_LABELS[kind]) for kind in kinds],
        # The picker starts on the first document still needed.
        "next_kind": missing[0][0] if missing else "other",
        "can_upload": live and status != S.LAPSED,
        "fee": services.fee_state(application),
        "fee_first": live
        and application.round_class.admission_round.fee_before_assessment
        and application.round_class.assessment != "none",
        # A test or interview still to come: once it is recorded, the family's page moves on.
        "sittings": [
            result.assessment
            for result in application.assessment_results.select_related("assessment").filter(
                attended__isnull=True, assessment__starts_at__gte=timezone.now() - timedelta(hours=12)
            )
        ]
        if live
        else [],
        "can_withdraw": status in WITHDRAWABLE,
        "offer": status == S.OFFERED,
        "tone": {
            S.OFFERED: "emerald",
            S.ACCEPTED: "emerald",
            S.ENROLLED: "emerald",
            S.WAITLISTED: "amber",
            S.NOT_OFFERED: "slate",
            S.LAPSED: "slate",
            S.WITHDRAWN: "slate",
            S.OFFER_DECLINED: "slate",
        }.get(status, "indigo"),
    }


ACTIONS = {"withdraw": S.WITHDRAWN, "accept": S.ACCEPTED, "decline": S.OFFER_DECLINED}


@public
def mine(request, slug):
    school, switched = _enter(request, slug)
    if switched:
        return switched
    applications = _mine(request, school)
    if request.method == "POST":
        application = next((a for a in applications if str(a.pk) == request.POST.get("application")), None)
        action = request.POST.get("action", "")
        if application is None or action not in ("upload", *ACTIONS):
            raise Http404
        try:
            if action == "upload":
                if too_large(request):
                    raise ValidationError(gettext("That file is too large. Each file can be at most 10 MB."))
                upload = request.FILES.get("file")
                if not upload:
                    raise ValidationError(gettext("Choose a file to upload."))
                services.add_document(application=application, kind=request.POST.get("kind", ""), upload=upload)
                messages.success(request, gettext("Thank you. The document is with the school to check."))
            else:
                services.transition(application, ACTIONS[action])
                messages.success(
                    request,
                    {
                        "withdraw": gettext("The application is withdrawn."),
                        "accept": gettext("You have accepted the place. The school will be in touch."),
                        "decline": gettext("You have declined the place."),
                    }[action],
                )
        except ValidationError as refused:
            for message in refused.messages:
                messages.error(request, message)
        return _private(redirect("apply:mine", slug=school.slug))
    return _render(request, school, "admissions/public/mine.html", {"cards": [_card(a) for a in applications]})


@public
def slip(request, slug, pk):
    school, switched = _enter(request, slug)
    if switched:
        return switched
    application = next((a for a in _mine(request, school) if a.pk == pk), None)
    if application is None:
        raise Http404
    return _private(slip_pdf(application, _link(request, application, _links(request)[str(pk)])))
