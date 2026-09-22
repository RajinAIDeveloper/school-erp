"""
Double-entry accounting. Every financial event (fee payment, expense, salary, other income)
is a balanced JournalEntry with debit and credit lines. Posted entries are never edited;
corrections are made with reversing entries.
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

from core.models import SchoolScopedModel

ZERO = Decimal("0.00")


class Account(SchoolScopedModel):
    class Type(models.TextChoices):
        ASSET = "asset", "Asset"
        LIABILITY = "liability", "Liability"
        EQUITY = "equity", "Equity"
        INCOME = "income", "Income"
        EXPENSE = "expense", "Expense"

    code = models.CharField(max_length=10, help_text="e.g. 1010")
    name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=10, choices=Type.choices)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children")
    is_cash = models.BooleanField(default=False, help_text="Cash, bank or mobile-banking account")
    is_active = models.BooleanField(default=True)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["school", "code"], name="unique_account_code_per_school")]

    def __str__(self):
        return f"{self.code} - {self.name}"

    @property
    def debit_normal(self):
        return self.account_type in (self.Type.ASSET, self.Type.EXPENSE)

    def balance(self, start=None, end=None):
        lines = JournalLine.objects.filter(
            account=self, entry__status__in=[JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED]
        )
        if start:
            lines = lines.filter(entry__date__gte=start)
        if end:
            lines = lines.filter(entry__date__lte=end)
        agg = lines.aggregate(d=Sum("debit"), c=Sum("credit"))
        d, c = agg["d"] or ZERO, agg["c"] or ZERO
        return d - c if self.debit_normal else c - d


class JournalEntry(SchoolScopedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        REVERSED = "reversed", "Reversed"

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual journal"
        FEE = "fee", "Fee payment"
        EXPENSE = "expense", "Expense"
        INCOME = "income", "Other income"
        SALARY = "salary", "Salary"
        REVERSAL = "reversal", "Reversal"
        OPENING = "opening", "Opening balance"

    entry_no = models.PositiveIntegerField()
    date = models.DateField()
    narration = models.CharField(max_length=250)
    reference = models.CharField(max_length=100, blank=True, help_text="Voucher / receipt / bill number")
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.MANUAL)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reversed_by"
    )

    class Meta:
        ordering = ["-date", "-entry_no"]
        constraints = [models.UniqueConstraint(fields=["school", "entry_no"], name="unique_entry_no_per_school")]
        verbose_name_plural = "journal entries"

    def __str__(self):
        return f"JE-{self.entry_no:05d} {self.narration}"

    @property
    def total_debit(self):
        return self.lines.aggregate(s=Sum("debit"))["s"] or ZERO

    @property
    def total_credit(self):
        return self.lines.aggregate(s=Sum("credit"))["s"] or ZERO

    @property
    def reversible(self):
        """Manual, expense and income entries are corrected here; fees and salaries are not."""
        return self.status == self.Status.POSTED and self.source in (
            self.Source.MANUAL,
            self.Source.EXPENSE,
            self.Source.INCOME,
            self.Source.OPENING,
        )

    @property
    def is_balanced(self):
        return self.total_debit == self.total_credit and self.total_debit > ZERO

    @staticmethod
    def next_entry_no(school):
        last = JournalEntry.objects.filter(school=school).aggregate(m=models.Max("entry_no"))["m"]
        return (last or 0) + 1

    def post(self):
        if self.status == self.Status.POSTED:
            return
        if self.status == self.Status.REVERSED:
            raise ValidationError("A reversed entry cannot be reposted.")
        if not self.is_balanced:
            raise ValidationError("Journal entry is not balanced or has no lines.")
        self.status = self.Status.POSTED
        self.save(update_fields=["status", "updated_at"])

    @transaction.atomic
    def reverse(self, user=None, narration=None):
        """Create a mirrored posted entry that cancels this one."""
        if self.status != self.Status.POSTED:
            raise ValidationError("Only posted entries can be reversed.")
        rev = JournalEntry.objects.create(
            school=self.school,
            entry_no=JournalEntry.next_entry_no(self.school),
            date=timezone.localdate(),
            narration=narration or f"Reversal of JE-{self.entry_no:05d}: {self.narration}",
            reference=self.reference,
            source=self.Source.REVERSAL,
            created_by=user,
            reverses=self,
        )
        for line in self.lines.all():
            JournalLine.objects.create(
                entry=rev, account=line.account, debit=line.credit, credit=line.debit, description=line.description
            )
        rev.post()
        self.status = self.Status.REVERSED
        self.save(update_fields=["status", "updated_at"])
        return rev


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-debit", "id"]

    def clean(self):
        if self.debit and self.credit:
            raise ValidationError("A line can have either a debit or a credit, not both.")
        if not self.debit and not self.credit:
            raise ValidationError("A line needs a debit or a credit amount.")

    def __str__(self):
        return f"{self.account} Dr {self.debit} Cr {self.credit}"


DEFAULT_ACCOUNTS = [
    # code, name, type, is_cash
    ("1010", "Cash in Hand", Account.Type.ASSET, True),
    ("1020", "Bank Account", Account.Type.ASSET, True),
    ("1030", "Mobile Banking (bKash/Nagad)", Account.Type.ASSET, True),
    ("1100", "Fees Receivable", Account.Type.ASSET, False),
    ("2010", "Salaries Payable", Account.Type.LIABILITY, False),
    ("3010", "Capital / Retained Earnings", Account.Type.EQUITY, False),
    ("4010", "Tuition Fee Income", Account.Type.INCOME, False),
    ("4020", "Admission Fee Income", Account.Type.INCOME, False),
    ("4030", "Exam Fee Income", Account.Type.INCOME, False),
    ("4040", "Transport Fee Income", Account.Type.INCOME, False),
    ("4090", "Other Fee Income", Account.Type.INCOME, False),
    ("4100", "Donations & Grants", Account.Type.INCOME, False),
    ("4900", "Other Income", Account.Type.INCOME, False),
    ("5010", "Salaries & Wages", Account.Type.EXPENSE, False),
    ("5020", "Rent", Account.Type.EXPENSE, False),
    ("5030", "Utilities (Electricity, Water, Internet)", Account.Type.EXPENSE, False),
    ("5040", "Stationery & Printing", Account.Type.EXPENSE, False),
    ("5050", "Repairs & Maintenance", Account.Type.EXPENSE, False),
    ("5060", "Transport", Account.Type.EXPENSE, False),
    ("5070", "SMS & Communication", Account.Type.EXPENSE, False),
    ("5900", "Miscellaneous Expense", Account.Type.EXPENSE, False),
]


def ensure_default_accounts(school):
    for code, name, typ, is_cash in DEFAULT_ACCOUNTS:
        Account.objects.get_or_create(
            school=school, code=code, defaults={"name": name, "account_type": typ, "is_cash": is_cash}
        )


def get_account(school, code):
    return Account.objects.get(school=school, code=code)


class Payroll(SchoolScopedModel):
    employee = models.ForeignKey("employees.Employee", on_delete=models.PROTECT, related_name="payrolls")
    month = models.DateField(help_text="First day of the salary month")
    basic = models.DecimalField(max_digits=12, decimal_places=2)
    allowance = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    deduction = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    paid_on = models.DateField(null=True, blank=True)
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, null=True, blank=True)

    class Meta:
        ordering = ["-month", "employee__employee_id"]
        constraints = [models.UniqueConstraint(fields=["employee", "month"], name="one_payroll_per_employee_month")]

    @property
    def net(self):
        return self.basic + self.allowance - self.deduction

    def clean(self):
        super().clean()
        if self.month and self.month.day != 1:
            raise ValidationError({"month": "Use the first day of the salary month."})
        if self.basic is not None and self.allowance is not None and self.deduction is not None:
            if min(self.basic, self.allowance, self.deduction) < 0 or self.net < 0:
                raise ValidationError("Salary components and net pay must not be negative.")

    def __str__(self):
        return f"{self.employee} - {self.month:%B %Y}"


@transaction.atomic
def record_simple_entry(
    school, date, narration, debit_account, credit_account, amount, *, source, reference="", user=None
):
    """Create and post a two-line entry: Dr debit_account / Cr credit_account."""
    amount = Decimal(amount)
    from core.access import assert_school
    from core.models import School

    assert_school(school, debit_account, credit_account)
    School.objects.select_for_update().get(pk=school.pk)
    if debit_account.pk == credit_account.pk:
        raise ValidationError("Debit and credit accounts must differ.")
    if not amount.is_finite() or amount <= ZERO:
        raise ValidationError("Amount must be positive.")
    entry = JournalEntry.objects.create(
        school=school,
        entry_no=JournalEntry.next_entry_no(school),
        date=date,
        narration=narration,
        reference=reference,
        source=source,
        created_by=user,
    )
    JournalLine.objects.create(entry=entry, account=debit_account, debit=amount)
    JournalLine.objects.create(entry=entry, account=credit_account, credit=amount)
    entry.post()
    return entry
