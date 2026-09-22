"""Deliver queued messages. Run it from a scheduler, or with --loop as a small worker."""

import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from messaging.backends import get_backend
from messaging.models import SMSMessage

MAX_ATTEMPTS = 3


class Command(BaseCommand):
    help = (
        "Process queued SMS messages. Each message is claimed before sending so two "
        "workers never send it twice, and a message that keeps failing is given up on "
        "after three attempts rather than texting a family in a loop."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--loop", action="store_true", help="Keep running instead of exiting.")
        parser.add_argument("--interval", type=int, default=30, help="Seconds between passes when looping.")

    def handle(self, *args, **options):
        while True:
            sent, failed = self.pass_once(options["limit"])
            self.stdout.write(f"{timezone.now():%H:%M:%S} sent {sent}; failed {failed}.")
            if not options["loop"]:
                return
            time.sleep(max(options["interval"], 1))

    def pass_once(self, limit):
        backend = get_backend()
        sent = failed = 0
        ids = list(
            SMSMessage.objects.filter(status=SMSMessage.Status.QUEUED, attempts__lt=MAX_ATTEMPTS)
            .order_by("pk")
            .values_list("pk", flat=True)[: max(0, limit)]
        )
        for pk in ids:
            with transaction.atomic():
                claimed = SMSMessage.objects.filter(pk=pk, status=SMSMessage.Status.QUEUED).update(
                    status=SMSMessage.Status.PROCESSING
                )
            if not claimed:
                continue
            message = SMSMessage.objects.get(pk=pk)
            message.attempts += 1
            message.save(update_fields=["attempts"])
            if backend.deliver(message):
                sent += 1
                continue
            failed += 1
            if message.attempts < MAX_ATTEMPTS:
                # Put it back for the next pass; a gateway hiccup should not lose a message.
                SMSMessage.objects.filter(pk=pk).update(status=SMSMessage.Status.QUEUED)
        return sent, failed
