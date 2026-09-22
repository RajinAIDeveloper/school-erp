from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import require_permission, students_for
from core.exports import pdf_response, spreadsheet
from core.generic import ERPListView

from .forms import GenerateInvoicesForm, PaymentForm
from .models import FeeInvoice, FeePayment
from .services import cancel_payment, collect_payment, generate_invoices


class InvoiceListView(ERPListView):
    model = FeeInvoice
    permission_required = "fees.view_feeinvoice"
    page_title = "Fee invoices"
    columns = (
        ("Invoice", "invoice_no"),
        ("Student", "student"),
        ("Due date", "due_date"),
        ("Total", "total", "money"),
        ("Paid", "paid", "money"),
        ("Balance", "balance", "money"),
        ("Status", "status", "badge"),
    )
    detail_url_name = "fees:invoice_detail"
    search_fields = ("invoice_no", "student__student_id", "student__first_name", "student__last_name")
    filters = (("status", "Status", FeeInvoice.Status.choices),)
    extra_actions = (
        ("Generate invoices", "fees:generate", "fees.add_feeinvoice"),
        ("Fee categories", "fees:category_list", "fees.view_feecategory"),
        ("Structures", "fees:structure_list", "fees.view_feestructure"),
        ("Concessions", "fees:concession_list", "fees.view_feeconcession"),
        ("Collection report", "fees:report", "fees.view_feepayment"),
    )


@require_permission("fees.add_feeinvoice")
def generate(request):
    form = GenerateInvoicesForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        try:
            count, skipped = generate_invoices(school=request.school, user=request.user, **form.cleaned_data)
            messages.success(request, f"Created {count} invoices; skipped {skipped}.")
            return redirect("fees:invoice_list")
        except ValidationError as e:
            form.add_error(None, e)
    return render(request, "generic/form.html", {"form": form, "page_title": "Generate class invoices"})


def invoice_queryset(request):
    qs = FeeInvoice.objects.filter(school=request.school)
    if not request.user.has_perm("fees.view_feeinvoice"):
        qs = qs.filter(student__in=students_for(request.user, request.school))
    return qs


@require_permission(None)
def detail(request, pk):
    inv = get_object_or_404(invoice_queryset(request), pk=pk)
    form = PaymentForm(request.POST or None, invoice=inv, school=request.school)
    if request.method == "POST":
        if not request.user.has_perm("fees.add_feepayment"):
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied
        if form.is_valid():
            try:
                p = collect_payment(school=request.school, user=request.user, invoice=inv, **form.cleaned_data)
                messages.success(request, f"Payment collected. Receipt {p.receipt_no}.")
                return redirect("fees:invoice_detail", pk=pk)
            except ValidationError as e:
                form.add_error(None, e)
    return render(
        request,
        "fees/detail.html",
        {
            "invoice": inv,
            "form": form,
            "page_title": inv.invoice_no,
            "items": inv.items.select_related("category"),
            "payments": inv.payments.all(),
        },
    )


@require_permission(None)
def receipt(request, pk):
    p = get_object_or_404(FeePayment, pk=pk, school=request.school, invoice__in=invoice_queryset(request))
    return pdf_response(
        "Fee receipt " + p.receipt_no,
        ["Invoice", "Student", "Date", "Method", "Amount", "Status"],
        [
            [
                p.invoice.invoice_no,
                p.invoice.student.full_name,
                str(p.date),
                p.method,
                p.amount,
                "CANCELLED" if p.is_cancelled else "Received",
            ]
        ],
        request.school.name,
    )


@require_permission("fees.change_feepayment")
@require_POST
def cancel(request, pk):
    payment = get_object_or_404(FeePayment, school=request.school, pk=pk)
    try:
        cancel_payment(school=request.school, user=request.user, payment=payment, reason=request.POST.get("reason", ""))
        messages.success(request, "Payment reversed in the accounts ledger.")
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    return redirect("fees:invoice_detail", pk=payment.invoice_id)


@require_permission("fees.view_feepayment")
def report(request):
    from django import forms

    from core.forms import TailwindFormMixin

    class Filter(TailwindFormMixin, forms.Form):
        start = forms.DateField(required=False)
        end = forms.DateField(required=False)

    form = Filter(request.GET or None)
    qs = FeePayment.objects.filter(school=request.school, is_cancelled=False).select_related("invoice__student")
    if form.is_bound and form.is_valid():
        if form.cleaned_data["start"]:
            qs = qs.filter(date__gte=form.cleaned_data["start"])
        if form.cleaned_data["end"]:
            qs = qs.filter(date__lte=form.cleaned_data["end"])
    rows = [[p.receipt_no, p.invoice.student.full_name, p.date, p.amount, p.method] for p in qs]
    headers = ["Receipt", "Student", "Date", "Amount", "Method"]
    if request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet("collections", headers, rows, request.GET["format"])
    return render(
        request,
        "generic/report.html",
        {"page_title": "Fee collections", "headers": headers, "rows": rows, "form": form},
    )
