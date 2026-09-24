"""
Paying fees online through SSLCommerz (bKash, Nagad, Rocket, cards and banks), or through a
demonstration gateway in which no money moves.

The rules that keep the money right:

- A payment counts only when SSLCommerz confirms it through its validation API, server to
  server. What the browser brings back, and what the notification says, are never trusted
  on their own.
- The confirmed amount and currency must match what this system asked for.
- The browser's return and the notification can both arrive, more than once and in either
  order; the receipt is written exactly once.
- Money lands in a clearing account (1040): the gateway holds it until it settles to the
  school's bank, less its charges, and the school's own gateway contract governs that.
- The demonstration gateway is refused on a production server unless deliberately allowed.
"""

import json
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import students_for
from core.models import AuditLog, School

from .models import FeeInvoice, FeePayment, OnlinePayment

BASES = {True: "https://sandbox.sslcommerz.com", False: "https://securepay.sslcommerz.com"}
OK_STATUSES = ("VALID", "VALIDATED")


# ------------------------------------------------------------------ the gateway's HTTP API


def http_post(url, data):
    request = Request(url, data=urlencode(data).encode(), method="POST")
    with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https endpoints above
        return json.loads(response.read().decode("utf-8"))


def http_get(url, params):
    with urlopen(f"{url}?{urlencode(params)}", timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def demo_allowed():
    return settings.DEBUG or getattr(settings, "ALLOW_DEMO_PAYMENTS", False)


# ------------------------------------------------------------------ starting a payment


def may_pay(user, invoice):
    """Families pay their own children's invoices; fee staff may start one on a family's behalf."""
    if user.has_perm("fees.add_feepayment"):
        return True
    return students_for(user, invoice.school).filter(pk=invoice.student_id).exists()


@transaction.atomic
def start_online_payment(*, user, invoice, amount, urls):
    """
    Open a payment and return the address to send the payer to.

    `urls` maps success, fail, cancel, ipn and demo to absolute addresses for a transaction
    ID; the view builds them from the request.
    """
    school = School.objects.select_for_update().get(pk=invoice.school_id)
    if school.payment_gateway == "none":
        raise ValidationError("Online payment is not switched on for this school.")
    if school.payment_gateway == "demo" and not demo_allowed():
        raise ValidationError("The demonstration gateway is switched off on this server.")
    if school.payment_gateway == "sslcommerz" and not (school.sslcommerz_store_id and school.sslcommerz_store_password):
        raise ValidationError("The school's SSLCommerz store details are not set up yet.")
    if not may_pay(user, invoice):
        raise PermissionDenied
    invoice = FeeInvoice.objects.select_for_update().get(pk=invoice.pk, school=school)
    try:
        amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValidationError("Enter the amount to pay.") from None
    if invoice.status == FeeInvoice.Status.CANCELLED or not amount.is_finite() or not 0 < amount <= invoice.balance:
        raise ValidationError(f"Pay between 0.01 and the balance due, {invoice.balance}.")
    online = OnlinePayment.objects.create(
        school=school,
        invoice=invoice,
        tran_id=f"S{school.pk}-{uuid.uuid4().hex[:24].upper()}",
        gateway=school.payment_gateway,
        amount=amount,
        started_by=user,
    )
    AuditLog.objects.create(
        school=school, user=user, action="online_payment.started", description=f"{online.tran_id}: {amount}"
    )
    if school.payment_gateway == "demo":
        return urls["demo"](online.tran_id)
    student = invoice.student
    guardian = student.primary_guardian
    reply = http_post(
        f"{BASES[school.sslcommerz_sandbox]}/gwprocess/v4/api.php",
        {
            "store_id": school.sslcommerz_store_id,
            "store_passwd": school.sslcommerz_store_password,
            "total_amount": str(amount),
            "currency": "BDT",
            "tran_id": online.tran_id,
            "success_url": urls["success"](online.tran_id),
            "fail_url": urls["fail"](online.tran_id),
            "cancel_url": urls["cancel"](online.tran_id),
            "ipn_url": urls["ipn"](online.tran_id),
            "product_name": f"School fees {invoice.invoice_no}"[:250],
            "product_category": "education",
            "product_profile": "non-physical-goods",
            "shipping_method": "NO",
            "num_of_item": 1,
            "cus_name": (guardian.full_name if guardian else student.full_name)[:50],
            "cus_email": user.email or school.email or "fees@example.com",
            "cus_phone": (guardian.phone if guardian else "") or school.phone or "01700000000",
            "cus_add1": (school.address or "Bangladesh")[:50],
            "cus_city": "Bangladesh",
            "cus_postcode": "0000",
            "cus_country": "Bangladesh",
            "value_a": str(school.pk),
            "value_b": invoice.invoice_no,
        },
    )
    if reply.get("status") != "SUCCESS" or not reply.get("GatewayPageURL"):
        online.status, online.note = OnlinePayment.Status.FAILED, str(reply.get("failedreason") or "Not started")[:200]
        online.save(update_fields=["status", "note", "updated_at"])
        raise ValidationError(f"The payment gateway did not start the payment: {online.note}")
    return reply["GatewayPageURL"]


# ------------------------------------------------------------------ confirming it


def validate_with_gateway(school, val_id):
    """Ask SSLCommerz, server to server, what really happened to this payment."""
    return http_get(
        f"{BASES[school.sslcommerz_sandbox]}/validator/api/validationserverAPI.php",
        {
            "val_id": val_id,
            "store_id": school.sslcommerz_store_id,
            "store_passwd": school.sslcommerz_store_password,
            "v": 1,
            "format": "json",
        },
    )


@transaction.atomic
def settle(online, confirmation):
    """
    Record a confirmed payment exactly once, or say why it was not recorded.

    `confirmation` is the gateway's validation reply (or the demonstration gateway's
    stand-in). A payment the gateway confirms but that no longer fits the invoice (paid
    meanwhile at the office, say) is held for review rather than refused, because the money
    has been taken.
    """
    School.objects.select_for_update().get(pk=online.school_id)
    online = OnlinePayment.objects.select_for_update().get(pk=online.pk)
    if online.status == OnlinePayment.Status.PAID:
        return online
    status = str(confirmation.get("status", "")).upper()
    if status not in OK_STATUSES:
        if online.status == OnlinePayment.Status.STARTED:
            online.status, online.note = OnlinePayment.Status.FAILED, f"Gateway says {status or 'not valid'}"[:200]
            online.save(update_fields=["status", "note", "updated_at"])
        return online
    online.val_id = str(confirmation.get("val_id", ""))[:80]
    online.bank_tran_id = str(confirmation.get("bank_tran_id", ""))[:80]
    online.card_type = str(confirmation.get("card_type", ""))[:60]
    try:
        confirmed_amount = Decimal(str(confirmation.get("amount", "")))
    except InvalidOperation:
        confirmed_amount = None
    problem = ""
    if confirmation.get("tran_id") != online.tran_id:
        problem = "The gateway confirmed a different transaction."
    elif confirmed_amount is None or confirmed_amount != online.amount:
        problem = f"The gateway confirmed {confirmation.get('amount')} but {online.amount} was asked for."
    elif str(confirmation.get("currency", "")).upper() != "BDT":
        problem = f"The gateway confirmed a payment in {confirmation.get('currency')}, not taka."
    invoice = FeeInvoice.objects.select_for_update().get(pk=online.invoice_id)
    if not problem and (invoice.status == FeeInvoice.Status.CANCELLED or online.amount > invoice.balance):
        problem = f"Paid online, but the invoice now has {invoice.balance} due. Refund or apply it by hand."
    if problem:
        online.status, online.note = OnlinePayment.Status.REVIEW, problem[:200]
        online.save(update_fields=["status", "note", "val_id", "bank_tran_id", "card_type", "updated_at"])
        AuditLog.objects.create(
            school=online.school, action="online_payment.review", description=f"{online.tran_id}: {problem}"
        )
        return online
    payment = FeePayment.objects.create(
        school=online.school,
        invoice=invoice,
        receipt_no=FeePayment.next_receipt_no(online.school),
        amount=online.amount,
        method=FeePayment.Method.ONLINE,
        reference=(online.bank_tran_id or online.val_id or online.tran_id)[:100],
        date=timezone.localdate(),
    )
    payment.post_to_ledger(None)
    invoice.refresh_status()
    online.status, online.payment, online.completed_at = OnlinePayment.Status.PAID, payment, timezone.now()
    online.save(
        update_fields=["status", "payment", "completed_at", "val_id", "bank_tran_id", "card_type", "updated_at"]
    )
    AuditLog.objects.create(
        school=online.school,
        action="online_payment.paid",
        description=f"{online.tran_id}: {payment.receipt_no} for {online.amount}",
    )
    from .services import notify_payment

    transaction.on_commit(lambda: notify_payment(payment))
    return online


def finish(tran_id, outcome, val_id=""):
    """
    The payer's browser is back. A success is believed only after asking the gateway; a
    failure or cancellation just closes an attempt that has not already succeeded.
    """
    online = OnlinePayment.objects.select_related("school").filter(tran_id=tran_id).first()
    if online is None:
        return None
    if outcome == "success" and val_id and online.gateway == "sslcommerz":
        return settle(online, validate_with_gateway(online.school, val_id))
    if outcome in ("fail", "cancel"):
        with transaction.atomic():
            online = OnlinePayment.objects.select_for_update().get(pk=online.pk)
            if online.status == OnlinePayment.Status.STARTED:
                online.status = OnlinePayment.Status.CANCELLED if outcome == "cancel" else OnlinePayment.Status.FAILED
                online.save(update_fields=["status", "updated_at"])
    online.refresh_from_db()
    return online


def notification(tran_id, val_id):
    """The gateway's own notification (IPN): confirmed with the gateway before anything is written."""
    online = OnlinePayment.objects.select_related("school").filter(tran_id=tran_id).first()
    if online is None or online.gateway != "sslcommerz" or not val_id:
        return None
    return settle(online, validate_with_gateway(online.school, val_id))


def demo_finish(tran_id, outcome):
    """The demonstration gateway's answer. Refused unless the demonstration gateway is allowed here."""
    online = OnlinePayment.objects.select_related("school").filter(tran_id=tran_id).first()
    if online is None or online.gateway != "demo" or not demo_allowed():
        raise PermissionDenied
    if outcome != "success":
        return finish(tran_id, outcome)
    return settle(
        online,
        {
            "status": "VALID",
            "tran_id": online.tran_id,
            "amount": str(online.amount),
            "currency": "BDT",
            "val_id": f"DEMO-{online.tran_id}",
            "bank_tran_id": "DEMONSTRATION",
            "card_type": "Demonstration",
        },
    )
