from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models import DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from academics.models import AcademicYear, ClassLevel
from core.models import SchoolQuerySet, SchoolScopedModel
from students.models import Enrollment, Student

ZERO = Decimal("0.00")

MONTHS = [
    (1, "January"),
    (2, "February"),
    (3, "March"),
    (4, "April"),
    (5, "May"),
    (6, "June"),
    (7, "July"),
    (8, "August"),
    (9, "September"),
    (10, "October"),
    (11, "November"),
    (12, "December"),
]


class FeeCategory(SchoolScopedModel):
    name = models.CharField(max_length=100, help_text="e.g. Tuition Fee, Admission Fee, Exam Fee")
    income_account = models.ForeignKey(
        "finance.Account",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="fee_categories",
        help_text="Income account credited when this fee is collected",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "fee categories"
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_fee_category_per_school")]

    def __str__(self):
        return self.name


class FeeStructure(SchoolScopedModel):
    class Frequency(models.TextChoices):
        ONE_TIME = "one_time", "One time"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE, related_name="fee_structures")
    class_level = models.ForeignKey(ClassLevel, on_delete=models.CASCADE, related_name="fee_structures")
    category = models.ForeignKey(FeeCategory, on_delete=models.PROTECT, related_name="structures")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    frequency = models.CharField(max_length=10, choices=Frequency.choices, default=Frequency.MONTHLY)

    class Meta:
        ordering = ["academic_year", "class_level__order", "category__name"]
        constraints = [
            models.UniqueConstraint(fields=["academic_year", "class_level", "category"], name="unique_fee_structure")
        ]

    def __str__(self):
        return f"{self.class_level} {self.category} {self.amount} ({self.get_frequency_display()})"


class FeeConcession(SchoolScopedModel):
    """Scholarship / sibling discount / staff-child waiver for one student."""

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="concessions")
    category = models.ForeignKey(
        FeeCategory, null=True, blank=True, on_delete=models.CASCADE, help_text="Blank = applies to all categories"
    )
    percent = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO, help_text="0-100")
    fixed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    reason = models.CharField(max_length=200, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["student__student_id", "category__name"]

    def __str__(self):
        return f"{self.student} - {self.percent}% / {self.fixed_amount}"

    def apply(self, amount):
        discounted = amount - (amount * self.percent / 100) - self.fixed_amount
        return max(discounted, ZERO)


class FeeInvoiceQuerySet(SchoolQuerySet):
    def with_totals(self):
        """
        Annotate subtotal, paid and balance in SQL.

        The model properties are correct but cost three queries per row, which turns any
        invoice list or dues report into hundreds of round trips. Lists and reports use
        this; single-object screens can still read the properties.
        """
        money = DecimalField(max_digits=14, decimal_places=2)
        items = (
            FeeInvoiceItem.objects.filter(invoice=OuterRef("pk"))
            .order_by()
            .values("invoice")
            .annotate(total=Sum("amount"))
            .values("total")
        )
        payments = (
            FeePayment.objects.filter(invoice=OuterRef("pk"), is_cancelled=False)
            .order_by()
            .values("invoice")
            .annotate(total=Sum("amount"))
            .values("total")
        )
        zero = Value(ZERO, output_field=money)
        return (
            self.annotate(
                subtotal_amount=Coalesce(Subquery(items, output_field=money), zero),
                paid_amount=Coalesce(Subquery(payments, output_field=money), zero),
            )
            .annotate(
                total_amount=F("subtotal_amount") - F("discount") + F("late_fee"),
            )
            .annotate(
                balance_amount=F("total_amount") - F("paid_amount"),
            )
        )

    def outstanding(self):
        return self.with_totals().filter(~Q(status="cancelled"), balance_amount__gt=ZERO)


class FeeInvoice(SchoolScopedModel):
    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    invoice_no = models.CharField(max_length=30)
    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name="invoices")
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT, related_name="invoices")
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.PROTECT, related_name="invoices")
    month = models.PositiveSmallIntegerField(choices=MONTHS, null=True, blank=True, help_text="For monthly fees")
    issue_date = models.DateField()
    due_date = models.DateField()
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    late_fee = models.DecimalField(max_digits=12, decimal_places=2, default=ZERO)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNPAID)
    notes = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    objects = FeeInvoiceQuerySet.as_manager()

    class Meta:
        ordering = ["-issue_date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "invoice_no"], name="unique_invoice_no_per_school"),
            models.UniqueConstraint(
                fields=["enrollment", "month"],
                condition=models.Q(month__isnull=False),
                name="one_invoice_per_enrollment_month",
            ),
        ]

    def __str__(self):
        return f"{self.invoice_no} - {self.student}"

    @property
    def subtotal(self):
        if hasattr(self, "subtotal_amount"):
            return self.subtotal_amount
        return self.items.aggregate(s=Sum("amount"))["s"] or ZERO

    @property
    def total(self):
        if hasattr(self, "total_amount"):
            return self.total_amount
        return self.subtotal - self.discount + self.late_fee

    @property
    def paid(self):
        if hasattr(self, "paid_amount"):
            return self.paid_amount
        return self.payments.filter(is_cancelled=False).aggregate(s=Sum("amount"))["s"] or ZERO

    @property
    def balance(self):
        if hasattr(self, "balance_amount"):
            return self.balance_amount
        return self.total - self.paid

    @property
    def is_overdue(self):
        from django.utils import timezone

        return self.balance > ZERO and self.status != self.Status.CANCELLED and self.due_date < timezone.localdate()

    @property
    def days_overdue(self):
        from django.utils import timezone

        return max((timezone.localdate() - self.due_date).days, 0) if self.is_overdue else 0

    def refresh_status(self):
        if self.status == self.Status.CANCELLED:
            return
        # Recompute from the database: an annotated instance carries the totals it was
        # loaded with, which are stale the moment a payment is written.
        for cached in ("subtotal_amount", "paid_amount", "total_amount", "balance_amount"):
            self.__dict__.pop(cached, None)
        paid, total = self.paid, self.total
        if total <= ZERO:
            self.status = self.Status.PAID
        elif paid <= ZERO:
            self.status = self.Status.UNPAID
        elif paid < total:
            self.status = self.Status.PARTIAL
        else:
            self.status = self.Status.PAID
        self.save(update_fields=["status", "updated_at"])

    @staticmethod
    def next_invoice_no(school, year_name):
        prefix = f"INV-{year_name}-"
        last = (
            FeeInvoice.objects.filter(school=school, invoice_no__startswith=prefix)
            .order_by("-invoice_no")
            .values_list("invoice_no", flat=True)
            .first()
        )
        n = int(last.split("-")[-1]) + 1 if last and last.split("-")[-1].isdigit() else 1
        return f"{prefix}{n:05d}"


class FeeInvoiceItem(models.Model):
    invoice = models.ForeignKey(FeeInvoice, on_delete=models.CASCADE, related_name="items")
    category = models.ForeignKey(FeeCategory, on_delete=models.PROTECT)
    description = models.CharField(max_length=150, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.category} {self.amount}"


class FeePayment(SchoolScopedModel):
    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer / cheque"
        MOBILE = "mobile", "Mobile banking (bKash / Nagad / Rocket)"

    METHOD_ACCOUNT_CODE = {"cash": "1010", "bank": "1020", "mobile": "1030"}

    receipt_no = models.CharField(max_length=30)
    invoice = models.ForeignKey(FeeInvoice, on_delete=models.PROTECT, related_name="payments")
    date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.CASH)
    reference = models.CharField(max_length=100, blank=True, help_text="Transaction ID / cheque no")
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    journal_entry = models.OneToOneField(
        "finance.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="fee_payment"
    )
    is_cancelled = models.BooleanField(default=False)
    cancel_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [models.UniqueConstraint(fields=["school", "receipt_no"], name="unique_receipt_no_per_school")]

    def __str__(self):
        return f"{self.receipt_no} {self.amount}"

    @staticmethod
    def next_receipt_no(school):
        prefix = "RCP-"
        last = (
            FeePayment.objects.filter(school=school, receipt_no__startswith=prefix)
            .order_by("-receipt_no")
            .values_list("receipt_no", flat=True)
            .first()
        )
        n = int(last.split("-")[-1]) + 1 if last and last.split("-")[-1].isdigit() else 1
        return f"{prefix}{n:06d}"

    @transaction.atomic
    def post_to_ledger(self, user=None):
        """Allocate partial receipts proportionally across their fee income accounts."""
        from decimal import ROUND_HALF_UP

        from django.core.exceptions import ValidationError

        from core.models import School
        from finance.models import Account, JournalEntry, JournalLine, ensure_default_accounts

        School.objects.select_for_update().get(pk=self.school_id)
        current = FeePayment.objects.select_for_update().get(pk=self.pk)
        if current.journal_entry_id:
            return current.journal_entry
        from finance.services import assert_period_open

        assert_period_open(self.school, self.date)
        ensure_default_accounts(self.school)
        debit = Account.objects.get(school=self.school, code=self.METHOD_ACCOUNT_CODE[self.method])
        fallback = Account.objects.get(school=self.school, code="4090")
        amounts = {}
        for item in self.invoice.items.select_related("category__income_account"):
            account = item.category.income_account or fallback
            if account.school_id != self.school_id or account.account_type != "income":
                raise ValidationError("Fee categories must use income accounts from the same school.")
            if item.amount > 0:
                amounts[account.pk] = amounts.get(account.pk, ZERO) + item.amount
        if not amounts:
            amounts[fallback.pk] = self.amount
        total = sum(amounts.values(), ZERO)
        entry = JournalEntry.objects.create(
            school=self.school,
            entry_no=JournalEntry.next_entry_no(self.school),
            date=self.date,
            narration=f"Fee receipt {self.receipt_no} - {self.invoice.student.full_name}"[:250],
            reference=self.receipt_no,
            source=JournalEntry.Source.FEE,
            created_by=user,
        )
        JournalLine.objects.create(entry=entry, account=debit, debit=self.amount)
        remaining = self.amount
        for index, (account_id, weight) in enumerate(amounts.items()):
            credit = (
                remaining
                if index == len(amounts) - 1
                else min(remaining, (self.amount * weight / total).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            )
            if credit:
                JournalLine.objects.create(entry=entry, account_id=account_id, credit=credit)
            remaining -= credit
        entry.post()
        self.journal_entry = entry
        self.save(update_fields=["journal_entry", "updated_at"])
        return entry

    @transaction.atomic
    def cancel(self, reason, user=None):
        if self.is_cancelled:
            return
        self.is_cancelled = True
        self.cancel_reason = reason
        self.save(update_fields=["is_cancelled", "cancel_reason", "updated_at"])
        if self.journal_entry_id and self.journal_entry.status == "posted":
            self.journal_entry.reverse(user=user, narration=f"Cancelled receipt {self.receipt_no}: {reason}")
        self.invoice.refresh_status()
