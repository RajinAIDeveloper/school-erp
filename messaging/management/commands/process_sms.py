"""
Deliver queued messages. Run it from a scheduler, or with --loop as a small worker.

Two things matter here beyond simply sending. A worker that dies mid-send must not strand
the message it was holding, and a gateway that is refusing everything must not be hammered
once a pass for the rest of the day.
"""

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from messaging.backends import get_backend
from messaging.models import MAX_ATTEMPTS, SMSMessage

# How long a claim may stand before we assume the worker holding it is gone. Long enough
# that a slow gateway call is never stolen mid-flight, short enough that a killed worker's
# messages go out on the next pass rather than the next day.
STALE_CLAIM_MINUTES = 15


def backoff(attempts):
    """Wait a minute, then five, then fifteen. A struggling gateway is given room."""
    return timedelta(seconds=min(60 * (5 ** max(attempts - 1, 0)), 900))


class Command(BaseCommand):
    help = (
        "Process queued SMS messages. Each message is claimed before sending so two "
        "workers never send it twice, a claim left behind by a crashed worker is "
        "reclaimed, and a message that keeps failing is backed off and then given up on "
        "rather than texting a family in a loop."
    )

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--loop", action="store_true", help="Keep running instead of exiting.")
        parser.add_argument("--interval", type=int, default=30, help="Seconds between passes when looping.")
        parser.add_argument(
            "--stale-minutes",
            type=int,
            default=STALE_CLAIM_MINUTES,
            help="Reclaim a message claimed but not finished within this many minutes.",
        )

    def handle(self, *args, **options):
        while True:
            reclaimed = self.reclaim_stale(options["stale_minutes"])
            sent, failed = self.pass_once(options["limit"])
            note = f"; reclaimed {reclaimed}" if reclaimed else ""
            self.stdout.write(f"{timezone.now():%H:%M:%S} sent {sent}; failed {failed}{note}.")
            if not options["loop"]:
                return
            time.sleep(max(options["interval"], 1))

    def reclaim_stale(self, stale_minutes):
        """
        Put back anything a worker claimed and never finished.

        The attempt was already counted when it was claimed, so a message that reliably
        kills the worker still runs out of attempts instead of being retried for ever.
        """
        cutoff = timezone.now() - timedelta(minutes=max(stale_minutes, 1))
        return SMSMessage.objects.filter(status=SMSMessage.Status.PROCESSING, claimed_at__lt=cutoff).update(
            status=SMSMessage.Status.QUEUED, claimed_at=None, updated_at=timezone.now()
        )

    def due(self, limit):
        now = timezone.now()
        return list(
            SMSMessage.objects.filter(
                Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
                status=SMSMessage.Status.QUEUED,
                attempts__lt=MAX_ATTEMPTS,
            )
            .order_by("pk")
            .values_list("pk", flat=True)[: max(0, limit)]
        )

    def pass_once(self, limit):
        backend = get_backend()
        sent = failed = 0
        for pk in self.due(limit):
            with transaction.atomic():
                # The claim and the attempt counter move together, in one conditional
                # update. Two workers racing for the same row: exactly one wins.
                claimed = SMSMessage.objects.filter(pk=pk, status=SMSMessage.Status.QUEUED).update(
                    status=SMSMessage.Status.PROCESSING,
                    claimed_at=timezone.now(),
                    attempts=F("attempts") + 1,
                    updated_at=timezone.now(),
                )
            if not claimed:
                continue
            message = SMSMessage.objects.get(pk=pk)
            if backend.deliver(message):
                sent += 1
                continue
            failed += 1
            if message.attempts < MAX_ATTEMPTS:
                SMSMessage.objects.filter(pk=pk).update(
                    status=SMSMessage.Status.QUEUED,
                    claimed_at=None,
                    next_attempt_at=timezone.now() + backoff(message.attempts),
                    updated_at=timezone.now(),
                )
        return sent, failed
