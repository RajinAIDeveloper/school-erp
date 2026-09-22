from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import require_permission
from core.exports import pdf_response, spreadsheet
from core.forms import TailwindFormMixin
from core.models import School, audit

from .models import Account, JournalEntry, Payroll, ensure_default_accounts, record_simple_entry


class JournalForm(TailwindFormMixin, forms.Form):
    date = forms.DateField(initial=timezone.localdate)
    narration = forms.CharField(max_length=250)
    debit_account = forms.ModelChoiceField(queryset=None)
    credit_account = forms.ModelChoiceField(queryset=None)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0.01)
    reference = forms.CharField(max_length=100, required=False)

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("debit_account", "credit_account"):
            self.fields[name].queryset = Account.objects.filter(school=school, is_active=True)


@require_permission("finance.view_journalentry")
def dashboard(request):
    entries = JournalEntry.objects.filter(school=request.school).prefetch_related("lines__account")
    return render(request, "finance/dashboard.html", {"entries": entries[:100], "page_title": "Accounts"})


@require_permission("finance.add_journalentry")
def journal_create(request):
    form = JournalForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                entry = record_simple_entry(request.school, **form.cleaned_data, source="manual", user=request.user)
                audit(request, "journal.posted", entry)
            return redirect("finance:dashboard")
        except ValidationError as e:
            form.add_error(None, e)
    return render(
        request, "generic/form.html", {"form": form, "page_title": "Post journal · income, expense or transfer"}
    )


@require_permission("finance.change_journalentry")
@require_POST
def reverse_entry(request, pk):
    entry = get_object_or_404(JournalEntry, school=request.school, pk=pk)
    if entry.source in ("fee", "salary"):
        messages.error(
            request,
            "Reverse fee payments through the collection desk; paid payroll corrections require an adjustment journal.",
        )
    elif not request.POST.get("reason", "").strip():
        messages.error(request, "A reason is required.")
    else:
        try:
            with transaction.atomic():
                School.objects.select_for_update().get(pk=request.school.pk)
                entry = JournalEntry.objects.select_for_update().get(pk=entry.pk)
                entry.reverse(request.user, request.POST["reason"])
                audit(request, "journal.reversed", entry, request.POST["reason"])
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
    return redirect("finance:dashboard")


@require_permission("finance.view_account")
def report(request):
    class Filter(TailwindFormMixin, forms.Form):
        start = forms.DateField(required=False)
        end = forms.DateField(required=False)

    form = Filter(request.GET or None)
    start = end = None
    if form.is_bound and form.is_valid():
        start, end = form.cleaned_data["start"], form.cleaned_data["end"]
    rows = [
        [a.code, a.name, a.account_type, a.balance(start, end)] for a in Account.objects.filter(school=request.school)
    ]
    headers = ["Code", "Account", "Type", "Balance"]
    if request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet("account-balances", headers, rows, request.GET["format"])
    return render(
        request,
        "generic/report.html",
        {"page_title": "Account balances / income and expenses", "form": form, "headers": headers, "rows": rows},
    )


@require_permission("finance.view_payroll")
def payroll(request):
    return render(
        request,
        "finance/payroll.html",
        {"rows": Payroll.objects.filter(school=request.school).select_related("employee"), "page_title": "Payroll"},
    )


@require_permission("finance.add_payroll")
def payroll_create(request):
    from core.forms import SchoolModelForm

    class Form(SchoolModelForm):
        class Meta:
            model = Payroll
            fields = ["employee", "month", "basic", "allowance", "deduction"]

    form = Form(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("finance:payroll")
    return render(request, "generic/form.html", {"form": form, "page_title": "Prepare salary"})


@require_permission("finance.change_payroll")
@require_POST
def payroll_pay(request, pk):
    with transaction.atomic():
        School.objects.select_for_update().get(pk=request.school.pk)
        row = get_object_or_404(Payroll.objects.select_for_update(), school=request.school, pk=pk)
        if not row.journal_entry_id:
            ensure_default_accounts(request.school)
            row.journal_entry = record_simple_entry(
                request.school,
                timezone.localdate(),
                str(row),
                Account.objects.get(school=request.school, code="5010"),
                Account.objects.get(school=request.school, code="1010"),
                row.net,
                source="salary",
                user=request.user,
            )
            row.paid_on = timezone.localdate()
            row.save()
            audit(request, "payroll.paid", row)
    return redirect("finance:payroll")


@require_permission("finance.view_payroll")
def payslip(request, pk):
    p = get_object_or_404(Payroll, school=request.school, pk=pk)
    return pdf_response(
        "Payslip",
        ["Employee", "Month", "Basic", "Allowance", "Deduction", "Net", "Paid on"],
        [[p.employee.full_name, p.month, p.basic, p.allowance, p.deduction, p.net, p.paid_on]],
        request.school.name,
    )
