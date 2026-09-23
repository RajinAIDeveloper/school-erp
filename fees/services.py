from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school
from core.models import AuditLog, School
from messaging.notifications import notify_payment
from students.models import Enrollment, Student

from .models import ZERO, FeeConcession, FeeInvoice, FeeInvoiceItem, FeePayment, FeeStructure


@transaction.atomic
def generate_invoices(*, school, user, academic_year, class_level=None, month, issue_date, due_date):
    assert_actor_school(user, school)
    assert_school(school, academic_year, class_level)
    if not user.has_perm("fees.add_feeinvoice"):
        raise PermissionDenied
    if month not in range(1, 13) or due_date < issue_date:
        raise ValidationError("Invalid month or dates.")
    School.objects.select_for_update().get(pk=school.pk)
    if class_level is None:
        # Bill the whole school in one run, reporting per class so the office can see
        # which classes have no fee structure configured yet.
        from academics.models import ClassLevel

        created = skipped = 0
        per_class = {}
        for level in ClassLevel.objects.filter(school=school, fee_structures__academic_year=academic_year).distinct():
            made, missed = generate_invoices(
                school=school,
                user=user,
                academic_year=academic_year,
                class_level=level,
                month=month,
                issue_date=issue_date,
                due_date=due_date,
            )
            per_class[level.name] = (made, missed)
            created += made
            skipped += missed
        if not per_class:
            raise ValidationError("No class has a fee structure for this academic year.")
        return created, skipped
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
        sibling_rate = sibling_discount_for(school, enr.student)
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
            if sibling_rate:
                amount = amount - (amount * sibling_rate / 100)
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


def sibling_discount_for(school, student):
    """
    The percentage a younger sibling is entitled to, or zero.

    Siblings are children who share a primary guardian. The eldest by date of birth pays
    in full; each younger active child gets the school's sibling rate. Ties on birthdate
    (twins) fall back to student ID so the answer is stable between runs.
    """
    rate = Decimal(school.sibling_discount_percent or 0)
    if rate <= ZERO:
        return ZERO
    link = student.guardian_links.filter(is_primary=True).select_related("guardian").first()
    if link is None:
        return ZERO
    siblings = list(
        Student.objects.filter(
            school=school,
            status=Student.Status.ACTIVE,
            guardian_links__guardian=link.guardian,
            guardian_links__is_primary=True,
        )
        .distinct()
        .order_by("date_of_birth", "student_id")
    )
    if len(siblings) < 2 or siblings[0].pk == student.pk:
        return ZERO
    return rate


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
    # Queued after commit: a texting fault must never undo a receipt.
    transaction.on_commit(lambda: notify_payment(payment))
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


@transaction.atomic
def create_invoice(
    *,
    school,
    user,
    student,
    enrollment,
    academic_year,
    month,
    issue_date,
    due_date,
    items,
    discount=ZERO,
    late_fee=ZERO,
    notes="",
):
    """
    Raise a one-off invoice, e.g. a re-sit fee or a replacement ID card, without waiting
    for the monthly run.
    """
    assert_actor_school(user, school)
    assert_school(school, student, enrollment, academic_year)
    if not user.has_perm("fees.add_feeinvoice"):
        raise PermissionDenied
    if due_date < issue_date:
        raise ValidationError("Due date cannot precede the issue date.")
    priced = [(category, Decimal(amount)) for category, amount in items if Decimal(amount) > ZERO]
    if not priced:
        raise ValidationError("Add at least one fee line with an amount.")
    for category, _amount in priced:
        assert_school(school, category)
    invoice = FeeInvoice.objects.create(
        school=school,
        invoice_no=FeeInvoice.next_invoice_no(school, academic_year.name),
        student=student,
        enrollment=enrollment,
        academic_year=academic_year,
        month=month,
        issue_date=issue_date,
        due_date=due_date,
        discount=Decimal(discount),
        late_fee=Decimal(late_fee),
        notes=notes,
        created_by=user,
    )
    FeeInvoiceItem.objects.bulk_create(
        [FeeInvoiceItem(invoice=invoice, category=category, amount=amount) for category, amount in priced]
    )
    invoice.refresh_status()
    AuditLog.objects.create(
        school=school,
        user=user,
        action="invoice.created",
        model=invoice._meta.label,
        object_id=str(invoice.pk),
        description=f"{invoice.invoice_no} for {student} ({invoice.total})",
    )
    return invoice


@transaction.atomic
def cancel_invoice(*, school, user, invoice, reason):
    """Void an invoice raised in error. Only possible while no money has been taken."""
    assert_actor_school(user, school)
    assert_school(school, invoice)
    if not user.has_perm("fees.change_feeinvoice"):
        raise PermissionDenied
    if not reason.strip():
        raise ValidationError("Give a reason for cancelling this invoice.")
    invoice = FeeInvoice.objects.select_for_update().get(pk=invoice.pk, school=school)
    if invoice.status == FeeInvoice.Status.CANCELLED:
        return invoice
    if invoice.payments.filter(is_cancelled=False).exists():
        raise ValidationError(
            "Money has been received against this invoice. Reverse the receipts first, so the "
            "ledger and the invoice stay in step."
        )
    invoice.status = FeeInvoice.Status.CANCELLED
    invoice.notes = f"{invoice.notes} | Cancelled: {reason}".strip(" |")
    invoice.save(update_fields=["status", "notes", "updated_at"])
    AuditLog.objects.create(
        school=school,
        user=user,
        action="invoice.cancelled",
        model=invoice._meta.label,
        object_id=str(invoice.pk),
        description=reason,
    )
    return invoice


def outstanding_invoices(school, *, class_level=None, section=None, as_of=None):
    """Unpaid and part-paid invoices for the dues report, ordered the way a roll is read."""
    as_of = as_of or timezone.localdate()
    qs = (
        FeeInvoice.objects.filter(school=school, issue_date__lte=as_of)
        .exclude(status=FeeInvoice.Status.CANCELLED)
        .with_totals()
        .filter(balance_amount__gt=ZERO)
        .select_related("student", "enrollment__section__class_level", "academic_year")
    )
    if class_level:
        qs = qs.filter(enrollment__class_level=class_level)
    if section:
        qs = qs.filter(enrollment__section=section)
    return qs.order_by("enrollment__section__class_level__order", "enrollment__roll_number", "due_date")


@transaction.atomic
def apply_late_fees(*, school, user, as_of=None, cap=None):
    """
    Charge the school's daily late fee on overdue invoices.

    The fee is recomputed from the number of overdue days rather than added to whatever is
    already recorded, so running this twice in a day cannot stack charges on a family.
    """
    assert_actor_school(user, school)
    if not user.has_perm("fees.change_feeinvoice"):
        raise PermissionDenied
    rate = Decimal(school.late_fee_per_day or 0)
    if rate <= ZERO:
        raise ValidationError("Set a daily late fee in Basic settings before running this.")
    as_of = as_of or timezone.localdate()
    cap = Decimal(cap) if cap is not None else Decimal(school.late_fee_cap or 0)
    changed = 0
    for invoice in outstanding_invoices(school, as_of=as_of).filter(due_date__lt=as_of):
        charge = rate * (as_of - invoice.due_date).days
        if cap > ZERO:
            charge = min(charge, cap)
        charge = charge.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if invoice.late_fee != charge:
            invoice.late_fee = charge
            invoice.save(update_fields=["late_fee", "updated_at"])
            invoice.refresh_status()
            changed += 1
    AuditLog.objects.create(
        school=school,
        user=user,
        action="fees.late_fee_applied",
        description=f"{changed} invoice(s) updated as of {as_of} at {rate}/day",
    )
    return changed


def send_due_reminders(*, school, user, invoices):
    """Queue one reminder per invoice. Sending itself stays the worker's job."""
    from messaging.notifications import notify_fee_due

    assert_actor_school(user, school)
    if not user.has_perm("messaging.add_smsmessage"):
        raise PermissionDenied
    if not school.notify_due_sms:
        raise ValidationError("Fee reminder SMS is switched off in Basic settings.")
    queued = notify_fee_due(school, invoices)
    AuditLog.objects.create(
        school=school, user=user, action="fees.reminders_queued", description=f"{queued} message(s)"
    )
    return queued
