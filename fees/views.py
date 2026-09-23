from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import require_permission, students_for
from core.exports import spreadsheet
from core.generic import ERPListView
from core.pdf import table_document

from .forms import (
    DuesFilterForm,
    GenerateInvoicesForm,
    InvoiceForm,
    InvoiceItemFormSet,
    LateFeeForm,
    PaymentForm,
)
from .models import FeeInvoice, FeePayment
from .services import (
    apply_late_fees,
    cancel_invoice,
    cancel_payment,
    collect_payment,
    create_invoice,
    edit_invoice,
    generate_invoices,
    outstanding_invoices,
    send_due_reminders,
)


class InvoiceListView(ERPListView):
    model = FeeInvoice
    select_related = ("student", "enrollment__section__class_level")
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
        ("Raise invoice", "fees:invoice_create", "fees.add_feeinvoice"),
        ("Outstanding", "fees:dues", "fees.view_feeinvoice"),
        ("Fee categories", "fees:category_list", "fees.view_feecategory"),
        ("Structures", "fees:structure_list", "fees.view_feestructure"),
        ("Concessions", "fees:concession_list", "fees.view_feeconcession"),
        ("Collection report", "fees:report", "fees.view_feepayment"),
    )

    def get_queryset(self):
        # Totals come from the annotation, not three property queries per row.
        return super().get_queryset().with_totals()


@require_permission("fees.add_feeinvoice")
def generate(request):
    form = GenerateInvoicesForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        try:
            count, skipped = generate_invoices(school=request.school, user=request.user, **form.cleaned_data)
            scope = form.cleaned_data["class_level"] or "every class"
            messages.success(
                request,
                f"Created {count} invoice(s) for {scope}; skipped {skipped} already billed or with nothing to charge.",
            )
            return redirect("fees:invoice_list")
        except ValidationError as e:
            form.add_error(None, e)
    return render(request, "generic/form.html", {"form": form, "page_title": "Generate class invoices"})


def invoice_queryset(request):
    qs = FeeInvoice.objects.filter(school=request.school)
    if not request.user.has_perm("fees.view_feeinvoice"):
        qs = qs.filter(student__in=students_for(request.user, request.school))
    return qs


@require_permission(None, also="own invoices unless you hold fees")
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
            "can_edit": (
                request.user.has_perm("fees.change_feeinvoice")
                and inv.status != FeeInvoice.Status.CANCELLED
                and not inv.payments.filter(is_cancelled=False).exists()
            ),
        },
    )


@require_permission(None, also="own receipts unless you hold fees")
def receipt(request, pk):
    from .documents import receipt_pdf

    payment = get_object_or_404(
        FeePayment.objects.select_related("invoice__student", "invoice__enrollment__section", "received_by"),
        pk=pk,
        school=request.school,
        invoice__in=invoice_queryset(request),
    )
    return receipt_pdf(request.school, payment, copy=request.GET.get("copy") == "1")


@require_permission(None, also="own children only")
def statement(request, student_pk):
    """Every charge and receipt for one student, for the office or the family."""
    from students.models import Student

    from .documents import statement_pdf

    visible = students_for(request.user, request.school)
    student = get_object_or_404(visible, pk=student_pk)
    invoices = (
        FeeInvoice.objects.filter(school=request.school, student=student)
        .exclude(status="cancelled")
        .with_totals()
        .order_by("issue_date")
    )
    payments = (
        FeePayment.objects.filter(school=request.school, invoice__student=student)
        .select_related("invoice")
        .order_by("date")
    )
    year = request.GET.get("year", "")
    if year:
        invoices = invoices.filter(academic_year__name=year)
        payments = payments.filter(invoice__academic_year__name=year)
    if request.GET.get("format") == "pdf":
        return statement_pdf(request.school, student, list(invoices), list(payments), period=year)
    charged = sum((i.total for i in invoices), start=Decimal("0.00"))
    received = sum((p.amount for p in payments if not p.is_cancelled), start=Decimal("0.00"))
    return render(
        request,
        "fees/statement.html",
        {
            "student": student,
            "invoices": invoices,
            "payments": payments,
            "charged": charged,
            "received": received,
            "balance": charged - received,
            "year": year,
            "years": Student.objects.none(),
            "page_title": f"Fee statement · {student.full_name}",
        },
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


@require_permission("fees.add_feeinvoice")
def invoice_create(request):
    """A one-off invoice, for charges the monthly run does not cover."""
    form = InvoiceForm(request.POST or None, school=request.school)
    items = InvoiceItemFormSet(request.POST or None, school=request.school, prefix="items")
    if request.method == "POST" and form.is_valid() and items.is_valid():
        lines = items.lines()
        try:
            invoice = create_invoice(
                school=request.school,
                user=request.user,
                student=form.cleaned_data["student"],
                enrollment=form.cleaned_data["enrollment"],
                academic_year=form.cleaned_data["academic_year"],
                month=form.cleaned_data.get("month") or None,
                issue_date=form.cleaned_data["issue_date"],
                due_date=form.cleaned_data["due_date"],
                items=lines,
                discount=form.cleaned_data["discount"],
                late_fee=form.cleaned_data["late_fee"],
                notes=form.cleaned_data["notes"],
            )
            messages.success(request, f"Raised invoice {invoice.invoice_no}.")
            return redirect("fees:invoice_detail", pk=invoice.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "fees/invoice_form.html",
        {"form": form, "items": items, "page_title": "Raise an invoice"},
    )


@require_permission("fees.change_feeinvoice")
def invoice_edit(request, pk):
    """Correct an invoice raised with the wrong figures, while no money has been taken."""
    invoice = get_object_or_404(
        FeeInvoice.objects.select_related("student", "enrollment", "academic_year"), school=request.school, pk=pk
    )
    if invoice.status == FeeInvoice.Status.CANCELLED or invoice.payments.filter(is_cancelled=False).exists():
        messages.error(
            request,
            "This invoice can no longer be edited: it is cancelled, or money has been received against it.",
        )
        return redirect("fees:invoice_detail", pk=pk)
    lines = list(invoice.items.select_related("category"))
    initial = {
        "student": invoice.student_id,
        "issue_date": invoice.issue_date,
        "due_date": invoice.due_date,
        "month": invoice.month,
        "discount": invoice.discount,
        "late_fee": invoice.late_fee,
        "notes": invoice.notes,
    }
    form = InvoiceForm(request.POST or None, school=request.school, invoice=invoice, initial=initial)
    items = InvoiceItemFormSet(
        request.POST or None,
        school=request.school,
        prefix="items",
        initial=[
            {"category": line.category_id, "description": line.description, "amount": line.amount} for line in lines
        ],
    )
    if request.method == "POST" and form.is_valid() and items.is_valid():
        try:
            edit_invoice(
                school=request.school,
                user=request.user,
                invoice=invoice,
                items=items.lines(),
                discount=form.cleaned_data["discount"],
                late_fee=form.cleaned_data["late_fee"],
                notes=form.cleaned_data["notes"],
                due_date=form.cleaned_data["due_date"],
                month=form.cleaned_data.get("month") or None,
            )
            messages.success(request, f"Updated invoice {invoice.invoice_no}.")
            return redirect("fees:invoice_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "fees/invoice_form.html",
        {
            "form": form,
            "items": items,
            "invoice": invoice,
            "page_title": f"Edit {invoice.invoice_no}",
            "submit_label": "Save changes",
        },
    )


@require_permission("fees.change_feeinvoice")
@require_POST
def invoice_cancel(request, pk):
    invoice = get_object_or_404(FeeInvoice, school=request.school, pk=pk)
    try:
        cancel_invoice(school=request.school, user=request.user, invoice=invoice, reason=request.POST.get("reason", ""))
        messages.success(request, f"Invoice {invoice.invoice_no} cancelled.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("fees:invoice_detail", pk=pk)


@require_permission("fees.view_feeinvoice")
def dues(request):
    """Who owes what, ready to chase."""
    form = DuesFilterForm(request.GET or None, school=request.school)
    invoices, totals = [], Decimal("0.00")
    if form.is_bound and form.is_valid():
        invoices = list(
            outstanding_invoices(
                request.school,
                class_level=form.cleaned_data["class_level"],
                section=form.cleaned_data["section"],
                as_of=form.cleaned_data["as_of"],
            )
        )
        if form.cleaned_data["overdue_only"]:
            invoices = [invoice for invoice in invoices if invoice.is_overdue]
    else:
        invoices = list(outstanding_invoices(request.school))
    totals = sum((invoice.balance for invoice in invoices), start=Decimal("0.00"))

    headers = ["Class", "Roll", "Student", "Guardian", "Phone", "Invoice", "Due date", "Days overdue", "Balance"]
    rows = [
        [
            str(invoice.enrollment.section),
            invoice.enrollment.roll_number,
            invoice.student.full_name,
            getattr(invoice.student.primary_guardian, "full_name", ""),
            getattr(invoice.student.primary_guardian, "phone", ""),
            invoice.invoice_no,
            invoice.due_date,
            invoice.days_overdue,
            invoice.balance,
        ]
        for invoice in invoices
    ]
    fmt = request.GET.get("format")
    if fmt == "pdf":
        return table_document(
            request.school,
            "Outstanding fees",
            headers,
            rows,
            subtitle=f"{len(rows)} invoice(s), {request.school.currency_symbol}{totals} outstanding",
            filename="fee-dues.pdf",
            align_right=(8,),
        )
    if fmt in ("csv", "xlsx"):
        return spreadsheet("fee-dues", headers, rows, fmt)
    return render(
        request,
        "fees/dues.html",
        {
            "form": form,
            "invoices": invoices,
            "total": totals,
            "page_title": "Outstanding fees",
            "can_remind": request.user.has_perm("messaging.add_smsmessage") and request.school.notify_due_sms,
        },
    )


@require_permission("messaging.add_smsmessage")
@require_POST
def remind(request):
    """Queue reminders for the invoices the user ticked, or for everything overdue."""
    selected = request.POST.getlist("invoice")
    invoices = outstanding_invoices(request.school)
    if selected:
        invoices = invoices.filter(pk__in=[pk for pk in selected if str(pk).isdigit()])
    try:
        queued = send_due_reminders(school=request.school, user=request.user, invoices=list(invoices))
        messages.success(request, f"Queued {queued} reminder(s). The worker will send them.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("fees:dues")


@require_permission("fees.change_feeinvoice")
def late_fees(request):
    form = LateFeeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            changed = apply_late_fees(
                school=request.school,
                user=request.user,
                as_of=form.cleaned_data["as_of"],
                cap=form.cleaned_data["cap"],
            )
            messages.success(request, f"Updated the late fee on {changed} invoice(s).")
            return redirect("fees:dues")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "generic/form.html",
        {
            "form": form,
            "page_title": "Apply late fees",
            "submit_label": "Apply",
            "intro": (
                f"Charges {request.school.currency_symbol}{request.school.late_fee_per_day} per overdue day. "
                "The fee is recalculated from the overdue days, so running this twice never stacks charges."
            ),
        },
    )
