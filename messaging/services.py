"""
Outgoing SMS.

Messages are queued here and sent by the worker, never inside a web request: a school's
gateway is a third party that can be slow or down, and a teacher saving a register should
never wait for it or lose their work to it.
"""

import re

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import SMSBatch, SMSMessage

# GSM-7 fits 160 characters per part; anything outside it (Bangla, emoji) is UCS-2 at 70.
GSM_SINGLE, GSM_MULTI = 160, 153
UNICODE_SINGLE, UNICODE_MULTI = 70, 67
GSM_CHARS = set(
    "@£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "\n\r\f[]{}\\~^|€"
)


def is_unicode(text):
    return any(character not in GSM_CHARS for character in text)


def segments(text):
    """How many SMS parts a body costs. Bangla text is Unicode, so it costs more."""
    if not text:
        return 0
    length = len(text)
    if is_unicode(text):
        return 1 if length <= UNICODE_SINGLE else -(-length // UNICODE_MULTI)
    return 1 if length <= GSM_SINGLE else -(-length // GSM_MULTI)


def describe_length(text):
    """A short line the compose screen can show under the message box."""
    parts = segments(text)
    limit = UNICODE_SINGLE if is_unicode(text) else GSM_SINGLE
    encoding = "Unicode" if is_unicode(text) else "GSM"
    return f"{len(text)} characters · {parts} SMS part{'s' if parts != 1 else ''} · {encoding} ({limit} per part)"


def normalize_bd_phone(phone):
    digits = re.sub(r"[^0-9]", "", str(phone))
    if digits.startswith("01"):
        digits = "88" + digits
    if not re.fullmatch(r"8801[3-9][0-9]{8}", digits):
        raise ValidationError(f"Invalid Bangladesh mobile number: {phone}")
    return digits


def render_body(body, context):
    """Substitute {student}, {class}, {amount} and friends for one recipient."""
    for placeholder, value in (context or {}).items():
        body = body.replace("{" + placeholder + "}", "" if value is None else str(value))
    return body


@transaction.atomic
def queue_batch(school, title, recipients_kind, body, contacts, user=None):
    """
    Queue one message per recipient.

    `contacts` is (name, phone) or (name, phone, context); a context renders the
    placeholders for that person, so one template can address a whole class personally.
    """
    if not body.strip() or len(body) > 480:
        raise ValidationError("Message must contain 1 to 480 characters.")
    # Keyed on the number AND the rendered text. A guardian with two children in the
    # same class gets a message about each child; the same generic notice still reaches
    # a number only once.
    messages = {}
    for contact in contacts:
        name, phone = contact[0], contact[1]
        context = contact[2] if len(contact) > 2 else {}
        if not phone:
            continue
        number = normalize_bd_phone(phone)
        rendered = render_body(body, context)
        messages.setdefault((number, rendered), name)
    if not messages:
        raise ValidationError("No recipients with valid phone numbers.")
    batch = SMSBatch.objects.create(school=school, title=title, recipients=recipients_kind, body=body, sent_by=user)
    SMSMessage.objects.bulk_create(
        [
            SMSMessage(school=school, batch=batch, recipient_name=name, phone=number, body=rendered)
            for (number, rendered), name in messages.items()
        ]
    )
    return batch


def send_single(school, phone, body, name=""):
    return SMSMessage.objects.create(school=school, phone=normalize_bd_phone(phone), body=body, recipient_name=name)


def send_now(message):
    """
    Deliver one message immediately through the configured gateway.

    Only the gateway test uses this: an administrator pressing "send a test" expects an
    answer on the screen, not a row in a queue.
    """
    from .backends import get_backend

    return get_backend().deliver(message)
