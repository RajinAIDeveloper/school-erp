from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from core.access import assert_actor_school, assert_school
from core.models import AuditLog, School
from students.models import Enrollment

from .models import FeeConcession, FeeInvoice, FeeInvoiceItem, FeePayment, FeeStructure


@transaction.atomic
def generate_invoices(*, school, user, academic_year, class_level, month, issue_date, due_date):
    assert_actor_school(user, school)
    assert_school(school, academic_year, class_level)
    if not user.has_perm("fees.add_feeinvoice"):
        raise PermissionDenied
    if month not in range(1, 13) or due_date < issue_date:
        raise ValidationError("Invalid month or dates.")
    School.objects.select_for_update().get(pk=school.pk)
    structures = list(
        FeeStructure.objects.filter(
            school=school, academic_year=academic_year, class_level=class_level, category__is_active=True
        ).select_related("category")
    )
    if not structures:
        raise ValidationError("Configure fees for this class and academic year first.")
    created = skipped = 0
    for enr in Enrollment.objects.filter(
        school=school, academic_year=academic_year, class_level=class_level, student__status="active", status="enrolled"
    ).select_related("student"):
        if FeeInvoice.objects.filter(enrollment=enr, month=month).exists():
            skipped += 1
            continue
        items = []
        concessions = list(FeeConcession.objects.filter(school=school, student=enr.student, is_active=True))
        for fs in structures:
            previous = FeeInvoiceItem.objects.filter(invoice__student=enr.student, category=fs.category).exclude(
                invoice__status="cancelled"
            )
            if fs.frequency == "one_time" and previous.exists():
                continue
            if fs.frequency == "yearly" and previous.filter(invoice__academic_year=academic_year).exists():
                continue
            if fs.frequency == "quarterly":
                quarter = (month - 1) // 3
                if previous.filter(
                    invoice__academic_year=academic_year, invoice__month__in=range(quarter * 3 + 1, quarter * 3 + 4)
                ).exists():
                    continue
            amount = fs.amount
            if amount < 0:
                raise ValidationError("Fee amounts cannot be negative.")
            for c in concessions:
                if c.category_id in (None, fs.category_id):
                    if c.percent < 0 or c.percent > 100 or c.fixed_amount < 0:
                        raise ValidationError("Invalid concession.")
                    amount = c.apply(amount)
            items.append((fs.category, amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))
        if not items:
            skipped += 1
            continue
        inv = FeeInvoice.objects.create(
            school=school,
            invoice_no=FeeInvoice.next_invoice_no(school, academic_year.name),
            student=enr.student,
            enrollment=enr,
            academic_year=academic_year,
            month=month,
            issue_date=issue_date,
            due_date=due_date,
            created_by=user,
        )
        FeeInvoiceItem.objects.bulk_create([FeeInvoiceItem(invoice=inv, category=c, amount=a) for c, a in items])
        inv.refresh_status()
        created += 1
    AuditLog.objects.create(
        school=school, user=user, action="invoices.generated", description=f"{created} created; {skipped} skipped."
    )
    return created, skipped


@transaction.atomic
def collect_payment(*, school, user, invoice, amount, method, reference, date):
    assert_actor_school(user, school)
    if not user.has_perm("fees.add_feepayment"):
        raise PermissionDenied
    assert_school(school, invoice)
    School.objects.select_for_update().get(pk=school.pk)
    invoice = FeeInvoice.objects.select_for_update().get(pk=invoice.pk, school=school)
    amount = Decimal(amount)
    if not amount.is_finite() or amount <= 0 or amount > invoice.balance or invoice.status == "cancelled":
        raise ValidationError("Payment is invalid or exceeds the outstanding balance.")
    if method not in FeePayment.Method.values or (method != "cash" and not reference):
        raise ValidationError("Select a payment method and provide its transaction reference.")
    payment = FeePayment.objects.create(
        school=school,
        invoice=invoice,
        receipt_no=FeePayment.next_receipt_no(school),
        amount=amount,
        method=method,
        reference=reference,
        date=date,
        received_by=user,
    )
    payment.post_to_ledger(user)
    invoice.refresh_status()
    AuditLog.objects.create(
        school=school,
        user=user,
        action="payment.collected",
        model=payment._meta.label,
        object_id=str(payment.pk),
        description=f"{payment.receipt_no}: {amount}",
    )
    return payment


@transaction.atomic
def cancel_payment(*, school, user, payment, reason):
    assert_actor_school(user, school)
    if not user.has_perm("fees.change_feepayment"):
        raise PermissionDenied
    assert_school(school, payment)
    if not reason.strip():
        raise ValidationError("A refund/cancellation reason is required.")
    School.objects.select_for_update().get(pk=school.pk)
    payment = FeePayment.objects.select_for_update().get(pk=payment.pk)
    payment.cancel(reason.strip(), user=user)
    AuditLog.objects.create(
        school=school, user=user, action="payment.cancelled", object_id=str(payment.pk), description=reason
    )
