"""
The admission office: rounds and their classes, each class's applications, one application's
page, walk-in applications, and the application fee at the counter.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.security import client_ip

from . import services
from .access import admissions_view
from .forms import DETAIL_FIELDS, OfficeApplicationForm, RoundClassForm, RoundForm
from .models import (
    DOCUMENT_LABELS,
    AdmissionRound,
    Application,
    ApplicationDocument,
    ApplicationPayment,
    RoundClass,
)

S = Application.Status
STAFF_LINKS = "admission_staff_links"  # {application pk: raw token}, for printing a slip once


def _refused(request, problem):
    for message in getattr(problem, "messages", [str(problem)]):
        messages.error(request, message)


def _keep_link(request, application, raw):
    held = dict(request.session.get(STAFF_LINKS, {}))
    held[str(application.pk)] = raw
    request.session[STAFF_LINKS] = held


# ------------------------------------------------------------------ rounds


@admissions_view("admissions.view_application")
def home(request):
    today = timezone.localdate()
    rounds = AdmissionRound.objects.filter(school=request.school).select_related("academic_year")
    counts = dict(
        Application.objects.filter(school=request.school)
        .values_list("round_class__admission_round")
        .annotate(n=Count("id"))
        .values_list("round_class__admission_round", "n")
    )
    query = (request.GET.get("q") or "").strip()
    found = []
    if query:
        found = list(
            Application.objects.filter(school=request.school, purged_at__isnull=True)
            .filter(
                Q(reference__iexact=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(guardian_phone__icontains=query)
                | Q(guardian_name__icontains=query)
            )
            .select_related("round_class__class_level", "round_class__admission_round")[:50]
        )
    return render(
        request,
        "admissions/home.html",
        {
            "rounds": [(item, counts.get(item.pk, 0), item.is_open(today)) for item in rounds],
            "query": query,
            "found": found,
            "public_url": request.build_absolute_uri(reverse("apply:landing", args=[request.school.slug])),
            "page_title": "Admissions",
        },
    )


@admissions_view("admissions.add_admissionround", also="the school's managers")
def round_new(request):
    return _round_form(request, AdmissionRound(school=request.school))


@admissions_view("admissions.change_admissionround", also="the school's managers")
def round_edit(request, pk):
    return _round_form(request, get_object_or_404(AdmissionRound, pk=pk, school=request.school))


def _round_form(request, instance):
    form = RoundForm(request.POST or None, instance=instance, school=request.school)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"{saved} saved.")
        return redirect("admissions:round", pk=saved.pk)
    return render(
        request,
        "admissions/round_form.html",
        {"form": form, "page_title": str(instance) if instance.pk else "New admission round"},
    )


@admissions_view("admissions.view_admissionround")
def round_detail(request, pk):
    admission_round = get_object_or_404(
        AdmissionRound.objects.select_related("academic_year"), pk=pk, school=request.school
    )
    today = timezone.localdate()
    classes = []
    for row in admission_round.classes.select_related("class_level"):
        by_status = dict(
            row.applications.filter(purged_at__isnull=True)
            .values_list("status")
            .annotate(n=Count("id"))
            .values_list("status", "n")
        )
        classes.append(
            {
                "row": row,
                "rule": services.birth_rule(row),
                "taken": services.seats_taken(row, today=today),
                "total": sum(by_status.values()),
                "waiting": by_status.get(S.SUBMITTED, 0) + by_status.get(S.UNDER_REVIEW, 0),
                "documents": [DOCUMENT_LABELS[kind] for kind in row.required_documents if kind in DOCUMENT_LABELS],
            }
        )
    return render(
        request,
        "admissions/round_detail.html",
        {
            "round": admission_round,
            "classes": classes,
            "is_open": admission_round.is_open(today),
            "page_title": str(admission_round),
        },
    )


@admissions_view("admissions.add_roundclass", also="the school's managers")
def class_new(request, pk):
    admission_round = get_object_or_404(AdmissionRound, pk=pk, school=request.school)
    return _class_form(request, admission_round, RoundClass(school=request.school))


@admissions_view("admissions.change_roundclass", also="the school's managers")
def class_edit(request, pk):
    row = get_object_or_404(RoundClass.objects.select_related("admission_round"), pk=pk, school=request.school)
    return _class_form(request, row.admission_round, row)


def _class_form(request, admission_round, instance):
    form = RoundClassForm(
        request.POST or None, instance=instance, school=request.school, admission_round=admission_round
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{form.instance.class_level} saved.")
        return redirect("admissions:round", pk=admission_round.pk)
    return render(
        request,
        "admissions/class_form.html",
        {
            "form": form,
            "round": admission_round,
            "page_title": f"{instance.class_level} · {admission_round}"
            if instance.pk
            else f"Add a class · {admission_round}",
        },
    )


# ------------------------------------------------------------------ applications


@admissions_view("admissions.view_application")
def pipeline(request, pk):
    row = get_object_or_404(
        RoundClass.objects.select_related("admission_round", "class_level"), pk=pk, school=request.school
    )
    today = timezone.localdate()
    applications = row.applications.filter(purged_at__isnull=True).select_related("possible_duplicate_of")
    counts = dict(applications.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    wanted = request.GET.get("status", "")
    if wanted in S.values:
        applications = applications.filter(status=wanted)
    rows = []
    for application in applications.order_by("submitted_at", "pk"):
        rows.append(
            {
                "application": application,
                "fee": services.fee_state(application),
                "missing": len(services.documents_missing(application)),
            }
        )
    return render(
        request,
        "admissions/pipeline.html",
        {
            "row": row,
            "rows": rows,
            "statuses": [(value, label, counts.get(value, 0)) for value, label in S.choices],
            "wanted": wanted,
            "taken": services.seats_taken(row, today=today),
            "total": sum(counts.values()),
            "page_title": f"{row.class_level} · {row.admission_round}",
        },
    )


@admissions_view("admissions.add_application")
def office_entry(request, pk):
    row = get_object_or_404(
        RoundClass.objects.select_related("admission_round__academic_year", "class_level"), pk=pk, school=request.school
    )
    form = OfficeApplicationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            application, raw = services.submit(
                round_class=row,
                details=form.details(),
                user=request.user,
                ip=client_ip(request) or None,
                age_override_reason=form.cleaned_data.get("age_override_reason", ""),
            )
        except ValidationError as problem:
            form.add_error(
                "date_of_birth" if services.age_problem(row, form.cleaned_data["date_of_birth"]) else None, problem
            )
        else:
            _keep_link(request, application, raw)
            messages.success(
                request, f"{application.reference} entered. Print the family's slip now: it holds their private link."
            )
            return redirect("admissions:application", pk=application.pk)
    return render(
        request,
        "admissions/office_form.html",
        {
            "form": form,
            "row": row,
            "rule": services.birth_rule(row),
            "page_title": f"Walk-in application · {row.class_level}",
        },
    )


@admissions_view("admissions.change_application")
def application_edit(request, pk):
    application = get_object_or_404(Application, pk=pk, school=request.school, purged_at__isnull=True)
    initial = {name: getattr(application, name) for name in DETAIL_FIELDS}
    form = OfficeApplicationForm(request.POST or None, initial=initial, office=False)
    if request.method == "POST" and form.is_valid():
        try:
            services.correct_details(user=request.user, application=application, details=form.details())
        except ValidationError as problem:
            form.add_error(None, problem)
        else:
            messages.success(request, "Details saved.")
            return redirect("admissions:application", pk=application.pk)
    return render(
        request,
        "admissions/office_form.html",
        {"form": form, "application": application, "page_title": f"Correct details · {application.reference}"},
    )


STAFF_MOVES = {
    "review": S.UNDER_REVIEW,
    "not_offered": S.NOT_OFFERED,
    "withdraw": S.WITHDRAWN,
    "accepted": S.ACCEPTED,
    "declined": S.OFFER_DECLINED,
}


@admissions_view("admissions.view_application")
def application_detail(request, pk):
    application = get_object_or_404(
        Application.objects.select_related(
            "round_class__admission_round__academic_year",
            "round_class__class_level",
            "possible_duplicate_of",
            "sibling",
            "sibling_verified_by",
            "fee_waived_by",
            "student",
        ),
        pk=pk,
        school=request.school,
        purged_at__isnull=True,
    )
    if request.method == "POST":
        _act(request, application)
        return redirect("admissions:application", pk=application.pk)
    fee = services.fee_state(application)
    sibling_matches = []
    if application.sibling_claimed and application.sibling is None and request.GET.get("sibling"):
        from students.models import Student

        term = request.GET["sibling"].strip()
        sibling_matches = list(
            Student.objects.filter(school=request.school, status=Student.Status.ACTIVE)
            .filter(Q(student_id__iexact=term) | Q(first_name__icontains=term) | Q(last_name__icontains=term))
            .order_by("first_name")[:20]
        )
    held = request.session.get(STAFF_LINKS, {})
    labels = dict(S.choices)
    return render(
        request,
        "admissions/application.html",
        {
            "application": application,
            "status": application.current_status,
            "moves": services.ALLOWED.get(application.current_status, set()),
            "S": S,
            "fee": fee,
            "payments": list(application.payments.select_related("received_by", "voided_by")),
            "documents": list(application.documents.select_related("checked_by")),
            "missing": services.documents_missing(application),
            "kinds": list(DOCUMENT_LABELS.items()),
            "events": [
                {"event": e, "was": labels.get(e.from_status, ""), "to": labels.get(e.to_status, "")}
                for e in application.events.select_related("by")[:100]
            ],
            "rule": services.birth_rule(application.round_class),
            "outside_window": not application.round_class.admits_birth_date(application.date_of_birth),
            "sibling_matches": sibling_matches,
            "sibling_term": request.GET.get("sibling", ""),
            "methods": ApplicationPayment.Method.choices,
            "slip_ready": str(application.pk) in held,
            "today": timezone.localdate(),
            "page_title": f"{application.reference} · {application.child_name}",
        },
    )


def _act(request, application):
    action = request.POST.get("action", "")
    user = request.user
    try:
        if action in STAFF_MOVES:
            services.transition(application, STAFF_MOVES[action], user=user, reason=request.POST.get("reason", ""))
            messages.success(request, "Status changed.")
        elif action == "note":
            services.add_note(user=user, application=application, text=request.POST.get("text", ""))
            messages.success(request, "Note added.")
        elif action in ("accept_document", "reject_document"):
            document = get_object_or_404(ApplicationDocument, pk=request.POST.get("document"), application=application)
            services.check_document(
                user=user, document=document, accept=action == "accept_document", reason=request.POST.get("reason", "")
            )
            messages.success(request, "Document checked.")
        elif action == "upload":
            from core.files import too_large

            if too_large(request) or not request.FILES.get("file"):
                raise ValidationError("Choose a file of at most 10 MB.")
            services.add_document(
                application=application, kind=request.POST.get("kind", ""), upload=request.FILES["file"], user=user
            )
            messages.success(request, "Document added.")
        elif action == "sibling":
            from students.models import Student

            student = get_object_or_404(Student, pk=request.POST.get("student"), school=request.school)
            services.confirm_sibling(user=user, application=application, student=student)
            messages.success(request, f"{student} confirmed as the brother or sister.")
        elif action == "clear_sibling":
            services.confirm_sibling(user=user, application=application, student=None)
            messages.success(request, "Sibling cleared.")
        elif action == "reissue":
            raw = services.reissue_link(user=user, application=application)
            _keep_link(request, application, raw)
            messages.success(
                request, "A new private link is ready. Print the slip for the family; the old link no longer works."
            )
        elif action == "pay":
            from datetime import date as _date

            try:
                paid_on = _date.fromisoformat(request.POST.get("date", "")) if request.POST.get("date") else None
            except ValueError:
                raise ValidationError("Give the date as shown in the date box.") from None
            payment = services.record_payment(
                user=user,
                application=application,
                amount=request.POST.get("amount", ""),
                method=request.POST.get("method", ""),
                reference=request.POST.get("reference", ""),
                date=paid_on,
            )
            messages.success(request, f"Receipt {payment.receipt_no} written.")
        elif action == "void":
            payment = get_object_or_404(ApplicationPayment, pk=request.POST.get("payment"), application=application)
            services.void_payment(user=user, payment=payment, reason=request.POST.get("reason", ""))
            messages.success(request, f"Receipt {payment.receipt_no} is void and its ledger entry reversed.")
        elif action == "waive":
            services.waive_fee(user=user, application=application, reason=request.POST.get("reason", ""))
            messages.success(request, "Fee waived.")
        else:
            raise Http404
    except ValidationError as problem:
        _refused(request, problem)
    except PermissionDenied:
        messages.error(request, "Your role cannot do that.")


@admissions_view("admissions.view_applicationdocument")
def document_file(request, pk):
    from core.files import serve

    document = get_object_or_404(
        ApplicationDocument.objects.select_related("application"), pk=pk, school=request.school
    )
    extension = ".pdf" if document.file_kind == "pdf" else ".jpg"
    return serve(document.file, document.file_kind, f"{document.application.reference}-{document.kind}{extension}")


@admissions_view("admissions.change_application", also="only right after the link is made")
def office_slip(request, pk):
    from .slip import slip_pdf

    application = get_object_or_404(Application, pk=pk, school=request.school, purged_at__isnull=True)
    raw = request.session.get(STAFF_LINKS, {}).get(str(pk))
    if not raw or services.hash_token(raw) != application.token_hash:
        messages.error(request, "The link is shown only once. Give the family a new link to print a slip.")
        return redirect("admissions:application", pk=pk)
    response = slip_pdf(application, request.build_absolute_uri(reverse("apply:open", args=[request.school.slug, raw])))
    response["Cache-Control"] = "no-store"
    return response


@admissions_view("admissions.view_applicationpayment")
def receipt(request, pk):
    from .receipt import receipt_pdf

    payment = get_object_or_404(
        ApplicationPayment.objects.select_related("application__round_class__class_level", "received_by"),
        pk=pk,
        school=request.school,
    )
    return receipt_pdf(payment)


@admissions_view("admissions.change_admissionround", also="the school's managers")
def settings_page(request):
    if request.method == "POST":
        raw = (request.POST.get("keep_months") or "").strip()
        if raw.isdigit() and 1 <= int(raw) <= 36:
            request.school.admissions_keep_months = int(raw)
            request.school.save(update_fields=["admissions_keep_months", "updated_at"])
            messages.success(request, "Admissions settings saved.")
            return redirect("admissions:settings")
        messages.error(request, "Keep applications for 1 to 36 months.")
    return render(
        request,
        "admissions/settings.html",
        {"keep_months": request.school.admissions_keep_months, "page_title": "Admissions settings"},
    )
