import re

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import SMSBatch, SMSMessage


def normalize_bd_phone(phone):
    digits = re.sub(r"[^0-9]", "", str(phone))
    if digits.startswith("01"):
        digits = "88" + digits
    if not re.fullmatch(r"8801[3-9][0-9]{8}", digits):
        raise ValidationError(f"Invalid Bangladesh mobile number: {phone}")
    return digits


@transaction.atomic
def queue_batch(school, title, recipients_kind, body, contacts, user=None):
    if not body.strip() or len(body) > 480:
        raise ValidationError("Message must contain 1 to 480 characters.")
    recipients = {}
    for name, phone in contacts:
        if phone:
            recipients[normalize_bd_phone(phone)] = name
    if not recipients:
        raise ValidationError("No recipients with valid phone numbers.")
    batch = SMSBatch.objects.create(school=school, title=title, recipients=recipients_kind, body=body, sent_by=user)
    SMSMessage.objects.bulk_create(
        [
            SMSMessage(school=school, batch=batch, recipient_name=name, phone=phone, body=body)
            for phone, name in recipients.items()
        ]
    )
    return batch


def send_single(school, phone, body, name=""):
    return SMSMessage.objects.create(school=school, phone=normalize_bd_phone(phone), body=body, recipient_name=name)
