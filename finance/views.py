"""Accounts: journals, ledgers, financial statements and payroll."""

from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import require_permission
from core.exports import spreadsheet
from core.forms import SchoolModelForm, TailwindFormMixin
from core.pdf import table_document

from . import reports
from .models import ZERO, Account, JournalEntry, Payroll
from .services import (
    close_books,
    generate_payroll,
    pay_payroll,
    post_journal,
    record_opening_balances,
    reverse_journal,
)


class PeriodForm(TailwindFormMixin, forms.Form):
    start = forms.DateField(required=False, initial=lambda: timezone.localdate().replace(month=1, day=1))
    end = forms.DateField(required=False, initial=timezone.localdate)

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            self.add_error("end", "The end date must follow the start date.")
        return data

    def period(self):
        if self.is_bound and self.is_valid():
            return self.cleaned_data.get("start"), self.cleaned_data.get("end")
        return timezone.localdate().replace(month=1, day=1), timezone.localdate()


class JournalLineForm(TailwindFormMixin, forms.Form):
    account = forms.ModelChoiceField(queryset=Account.objects.none(), required=False)
    description = forms.CharField(max_length=200, required=False)
    debit = forms.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)
    credit = forms.DecimalField(max_digits=14, decimal_places=2, required=False, min_value=0)

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        if school is not None:
            self.fields["account"].queryset = Account.objects.filter(school=school, is_active=True)

    def clean(self):
        data = super().clean()
        debit, credit = data.get("debit") or ZERO, data.get("credit") or ZERO
        if (debit or credit) and not data.get("account"):
            self.add_error("account", "Choose the account this amount belongs to.")
        if debit and credit:
            self.add_error("credit", "A line is either a debit or a credit.")
        return data


class BaseJournalLineFormSet(forms.BaseFormSet):
    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["school"] = self.school
        return kwargs

    def lines(self):
        return [
            (f.cleaned_data["account"], f.cleaned_data.get("debit") or ZERO, f.cleaned_data.get("credit") or ZERO)
            for f in self.forms
            if f.cleaned_data.get("account") and (f.cleaned_data.get("debit") or f.cleaned_data.get("credit"))
        ]


JournalLineFormSet = forms.formset_factory(
    JournalLineForm, formset=BaseJournalLineFormSet, extra=4, max_num=30, validate_max=True
)


class JournalForm(TailwindFormMixin, forms.Form):
    date = forms.DateField(initial=timezone.localdate)
    narration = forms.CharField(max_length=250)
    reference = forms.CharField(max_length=100, required=False, help_text="Voucher, bill or cheque number.")


class QuickEntryForm(TailwindFormMixin, forms.Form):
    """The two everyday cases: money spent, and money received that is not a school fee."""

    KIND = [("expense", "Expense paid"), ("income", "Other income received")]

    kind = forms.ChoiceField(choices=KIND)
    date = forms.DateField(initial=timezone.localdate)
    narration = forms.CharField(max_length=250, label="What was it for")
    category = forms.ModelChoiceField(queryset=Account.objects.none(), label="Expense or income head")
    paid_from = forms.ModelChoiceField(queryset=Account.objects.none(), label="Cash / bank account")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    reference = forms.CharField(max_length=100, required=False)

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = Account.objects.filter(
            school=school, is_active=True, account_type__in=[Account.Type.EXPENSE, Account.Type.INCOME]
        )
        self.fields["paid_from"].queryset = Account.objects.filter(school=school, is_active=True, is_cash=True)

    def clean(self):
        data = super().clean()
        kind, category = data.get("kind"), data.get("category")
        if kind and category:
            wanted = Account.Type.EXPENSE if kind == "expense" else Account.Type.INCOME
            if category.account_type != wanted:
                self.add_error("category", f"Choose a{'n' if wanted == 'income' else ''} {wanted} account.")
        return data

    def lines(self):
        data = self.cleaned_data
        amount = data["amount"]
        if data["kind"] == "expense":
            return [(data["category"], amount, ZERO), (data["paid_from"], ZERO, amount)]
        return [(data["paid_from"], amount, ZERO), (data["category"], ZERO, amount)]


class PayrollForm(SchoolModelForm):
    class Meta:
        model = Payroll
        fields = ["employee", "month", "basic", "allowance", "deduction"]


class PayrollRunForm(TailwindFormMixin, forms.Form):
    month = forms.DateField(label="Salary month", help_text="Use the first day of the month, e.g. 2026-09-01.")


class CloseBooksForm(TailwindFormMixin, forms.Form):
    through = forms.DateField(label="Close the books through")


@require_permission("finance.view_journalentry")
def dashboard(request):
    school = request.school
    today = timezone.localdate()
    month_start = today.replace(day=1)
    period = income = None
    if request.user.has_perm("finance.view_account"):
        income = reports.income_statement(school, month_start, today)
        period = reports.cash_book(school, month_start, today)
    entries = (
        JournalEntry.objects.filter(school=school)
        .select_related("created_by")
        .prefetch_related("lines__account")
        .order_by("-date", "-entry_no")[:100]
    )
    return render(
        request,
        "finance/dashboard.html",
        {
            "entries": entries,
            "income": income,
            "cash": period,
            "month_start": month_start,
            "page_title": "Accounts",
            "locked_until": school.books_locked_until,
        },
    )


@require_permission("finance.add_journalentry")
def journal_create(request):
    """A full entry of any shape, for anything the quick forms do not cover."""
    form = JournalForm(request.POST or None)
    lines = JournalLineFormSet(request.POST or None, school=request.school, prefix="lines")
    if request.method == "POST" and form.is_valid() and lines.is_valid():
        try:
            entry = post_journal(
                school=request.school,
                user=request.user,
                date=form.cleaned_data["date"],
                narration=form.cleaned_data["narration"],
                reference=form.cleaned_data["reference"],
                lines=lines.lines(),
            )
            messages.success(request, f"Posted {entry}.")
            return redirect("finance:dashboard")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "finance/journal_form.html",
        {"form": form, "lines": lines, "page_title": "Post a journal entry"},
    )


@require_permission("finance.add_journalentry")
def quick_entry(request):
    form = QuickEntryForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        try:
            entry = post_journal(
                school=request.school,
                user=request.user,
                date=form.cleaned_data["date"],
                narration=form.cleaned_data["narration"],
                reference=form.cleaned_data["reference"],
                lines=form.lines(),
                source=(
                    JournalEntry.Source.EXPENSE
                    if form.cleaned_data["kind"] == "expense"
                    else JournalEntry.Source.INCOME
                ),
            )
            messages.success(request, f"Recorded {entry}.")
            return redirect("finance:dashboard")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "generic/form.html",
        {
            "form": form,
            "page_title": "Record an expense or other income",
            "submit_label": "Record",
            "cancel_url": "/finance/",
            "intro": "For school fees use the collection desk, so the receipt and the ledger stay together.",
        },
    )


@require_permission("finance.change_journalentry")
@require_POST
def reverse_entry(request, pk):
    entry = get_object_or_404(JournalEntry, school=request.school, pk=pk)
    try:
        reverse_journal(school=request.school, user=request.user, entry=entry, reason=request.POST.get("reason", ""))
        messages.success(request, "Entry reversed. Both the original and the reversal stay on the record.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("finance:dashboard")


@require_permission("finance.add_journalentry")
def opening_balances(request):
    accounts = list(Account.objects.filter(school=request.school, is_active=True).exclude(code="3010").order_by("code"))
    if request.method == "POST":
        balances = []
        for account in accounts:
            raw = request.POST.get(f"amount-{account.pk}", "").strip()
            if raw:
                try:
                    balances.append((account, Decimal(raw)))
                except Exception:  # noqa: BLE001 - a typed figure that is not a number
                    messages.error(request, f"{account}: '{raw}' is not an amount.")
                    balances = None
                    break
        if balances is not None:
            try:
                entry = record_opening_balances(
                    school=request.school,
                    user=request.user,
                    date=request.POST.get("date") or timezone.localdate(),
                    balances=balances,
                )
                messages.success(request, f"Opening balances posted as {entry}.")
                return redirect("finance:trial_balance")
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
    return render(
        request,
        "finance/opening_balances.html",
        {"accounts": accounts, "today": timezone.localdate(), "page_title": "Opening balances"},
    )


@require_permission("finance.view_account")
def account_ledger(request, pk):
    account = get_object_or_404(Account, school=request.school, pk=pk)
    form = PeriodForm(request.GET or None)
    start, end = form.period()
    data = reports.ledger(account, start, end)
    headers = ["Date", "Entry", "Narration", "Reference", "Debit", "Credit", "Balance"]
    rows = [
        [
            r["date"],
            f"JE-{r['entry'].entry_no:05d}",
            r["narration"],
            r["reference"],
            r["debit"],
            r["credit"],
            r["balance"],
        ]
        for r in data["rows"]
    ]
    fmt = request.GET.get("format")
    if fmt == "pdf":
        return table_document(
            request.school,
            f"Ledger · {account}",
            headers,
            rows,
            subtitle=f"{start} to {end} · opening {data['opening']} · closing {data['closing']}",
            filename=f"ledger-{account.code}.pdf",
            align_right=(4, 5, 6),
        )
    if fmt in ("csv", "xlsx"):
        return spreadsheet(f"ledger-{account.code}", headers, rows, fmt)
    return render(
        request,
        "finance/ledger.html",
        {"form": form, "data": data, "start": start, "end": end, "page_title": f"Ledger · {account}"},
    )


def _statement_view(request, template, builder, title, filename, export_rows):
    form = PeriodForm(request.GET or None)
    start, end = form.period()
    data = builder(request.school, start, end)
    fmt = request.GET.get("format")
    if fmt in ("csv", "xlsx", "pdf"):
        headers, rows = export_rows(data)
        if fmt == "pdf":
            return table_document(
                request.school,
                title,
                headers,
                rows,
                subtitle=f"{start} to {end}",
                filename=filename,
                align_right=tuple(range(1, len(headers))),
            )
        return spreadsheet(filename.replace(".pdf", ""), headers, rows, fmt)
    return render(request, template, {"form": form, "data": data, "start": start, "end": end, "page_title": title})


@require_permission("finance.view_account")
def trial_balance(request):
    return _statement_view(
        request,
        "finance/trial_balance.html",
        reports.trial_balance,
        "Trial balance",
        "trial-balance.pdf",
        lambda data: (
            ["Code", "Account", "Debit", "Credit"],
            [[r["account"].code, r["account"].name, r["debit"], r["credit"]] for r in data["rows"]]
            + [["", "Total", data["total_debit"], data["total_credit"]]],
        ),
    )


@require_permission("finance.view_account")
def income_statement(request):
    return _statement_view(
        request,
        "finance/income_statement.html",
        reports.income_statement,
        "Income and expenditure",
        "income-statement.pdf",
        lambda data: (
            ["Section", "Account", "Amount"],
            [["Income", r["account"].name, r["amount"]] for r in data["income"]]
            + [["Expense", r["account"].name, r["amount"]] for r in data["expense"]]
            + [["", "Surplus", data["surplus"]]],
        ),
    )


@require_permission("finance.view_account")
def balance_sheet(request):
    form = PeriodForm(request.GET or None)
    _start, end = form.period()
    data = reports.balance_sheet(request.school, end)
    fmt = request.GET.get("format")
    if fmt in ("csv", "xlsx", "pdf"):
        headers = ["Section", "Account", "Amount"]
        rows = (
            [["Assets", r["account"].name, r["amount"]] for r in data["assets"]]
            + [["Liabilities", r["account"].name, r["amount"]] for r in data["liabilities"]]
            + [["Equity", r["account"].name, r["amount"]] for r in data["equity"]]
            + [["Equity", "Retained surplus", data["surplus"]]]
        )
        if fmt == "pdf":
            return table_document(
                request.school,
                "Balance sheet",
                headers,
                rows,
                subtitle=f"As at {end}",
                filename="balance-sheet.pdf",
                align_right=(2,),
            )
        return spreadsheet("balance-sheet", headers, rows, fmt)
    return render(
        request,
        "finance/balance_sheet.html",
        {"form": form, "data": data, "end": end, "page_title": "Balance sheet"},
    )


@require_permission("finance.view_account")
def cash_book(request):
    return _statement_view(
        request,
        "finance/cash_book.html",
        reports.cash_book,
        "Cash book",
        "cash-book.pdf",
        lambda data: (
            ["Date", "Received", "Paid", "Balance"],
            [[r["date"], r["received"], r["paid"], r["balance"]] for r in data["rows"]],
        ),
    )


@require_permission("finance.view_account")
def report(request):
    """Account balances for a period, the simplest overview of the books."""
    form = PeriodForm(request.GET or None)
    start, end = form.period()
    totals = reports.account_totals(request.school, start, end)
    rows = []
    for account in Account.objects.filter(school=request.school).order_by("code"):
        debit, credit = totals.get(account.pk, (ZERO, ZERO))
        rows.append(
            [
                account.code,
                account.name,
                account.get_account_type_display(),
                reports.signed_balance(account, debit, credit),
            ]
        )
    headers = ["Code", "Account", "Type", "Balance"]
    if request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet("account-balances", headers, rows, request.GET["format"])
    if request.GET.get("format") == "pdf":
        return table_document(
            request.school,
            "Account balances",
            headers,
            rows,
            subtitle=f"{start} to {end}",
            filename="account-balances.pdf",
            align_right=(3,),
        )
    return render(
        request,
        "generic/report.html",
        {"page_title": "Account balances", "form": form, "headers": headers, "rows": rows},
    )


@require_permission("finance.view_payroll")
def payroll(request):
    month = request.GET.get("month", "")
    rows = Payroll.objects.filter(school=request.school).select_related("employee", "journal_entry")
    if len(month) == 7 and month[:4].isdigit():
        rows = rows.filter(month__year=int(month[:4]), month__month=int(month[5:7]))
    return render(
        request,
        "finance/payroll.html",
        {
            "rows": rows,
            "month": month,
            "total": sum((row.net for row in rows), start=ZERO),
            "unpaid": sum((row.net for row in rows if not row.journal_entry_id), start=ZERO),
            "page_title": "Payroll",
        },
    )


@require_permission("finance.add_payroll")
def payroll_create(request):
    form = PayrollForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Salary prepared. It is not paid until you record the payment.")
        return redirect("finance:payroll")
    return render(request, "generic/form.html", {"form": form, "page_title": "Prepare a salary"})


@require_permission("finance.add_payroll")
def payroll_run(request):
    form = PayrollRunForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            created = generate_payroll(school=request.school, user=request.user, month=form.cleaned_data["month"])
            messages.success(request, f"Prepared {created} salary row(s) from contracted basic pay.")
            return redirect("finance:payroll")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "generic/form.html",
        {
            "form": form,
            "page_title": "Prepare the month's salaries",
            "submit_label": "Prepare",
            "intro": "Creates a row for every active employee who does not already have one for that month.",
        },
    )


@require_permission("finance.change_payroll")
@require_POST
def payroll_pay(request, pk):
    row = get_object_or_404(Payroll, school=request.school, pk=pk)
    try:
        pay_payroll(
            school=request.school,
            user=request.user,
            payroll=row,
            method=request.POST.get("method", "cash"),
        )
        messages.success(request, f"Recorded the salary payment for {row.employee.full_name}.")
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    return redirect("finance:payroll")


@require_permission("finance.view_payroll")
def payslip(request, pk):
    from core.money import amount_in_words

    row = get_object_or_404(Payroll.objects.select_related("employee"), school=request.school, pk=pk)
    headers = ["Item", "Amount"]
    rows = [
        ["Basic", row.basic],
        ["Allowance", row.allowance],
        ["Deduction", f"-{row.deduction}"],
        ["Net pay", row.net],
    ]
    return table_document(
        request.school,
        f"Payslip · {row.month:%B %Y}",
        headers,
        rows,
        subtitle=(
            f"{row.employee.full_name} ({row.employee.employee_id}) · "
            f"{'Paid ' + row.paid_on.strftime('%d %b %Y') if row.paid_on else 'Not yet paid'} · "
            f"{amount_in_words(row.net)}"
        ),
        filename=f"payslip-{row.employee.employee_id}-{row.month:%Y-%m}.pdf",
        align_right=(1,),
    )


@require_permission("core.change_school")
def close_period(request):
    form = CloseBooksForm(request.POST or None, initial={"through": request.school.books_locked_until})
    if request.method == "POST" and form.is_valid():
        try:
            close_books(school=request.school, user=request.user, through=form.cleaned_data["through"])
            messages.success(request, "Books closed. Corrections now belong in an open period.")
            return redirect("finance:dashboard")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "generic/form.html",
        {
            "form": form,
            "page_title": "Close the books",
            "submit_label": "Close",
            "intro": "Nothing may be posted on or before the date you set, including reversals.",
        },
    )


@require_permission("finance.view_account")
def integrity(request):
    """A short check that the ledger still holds together."""
    balance = reports.trial_balance(request.school)
    broken = reports.unbalanced_entries(request.school)
    return render(
        request,
        "finance/integrity.html",
        {"balance": balance, "broken": broken, "page_title": "Ledger integrity"},
    )
