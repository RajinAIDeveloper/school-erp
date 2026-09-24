"""
Send the weekly homework digest: one SMS per child with homework not handed in this week.

Only schools with the homework module and the digest switched on (Notifications) are
included, and only guardians who accept SMS. Run it once a week, before the weekend; running
it again in the same week sends nothing new.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import School
from core.modules import has_module
from messaging.notifications import notify_homework_digest


class Command(BaseCommand):
    help = "Queue the weekly homework digest SMS for every school that has switched it on."

    def add_arguments(self, parser):
        parser.add_argument("--school", help="Only this school (its slug).")
        parser.add_argument("--dry-run", action="store_true", help="Say which schools would send, and send nothing.")

    def handle(self, *args, **opts):
        schools = School.objects.filter(is_active=True)
        if opts["school"]:
            schools = schools.filter(slug=opts["school"])
            if not schools.exists():
                raise CommandError("No active school has that slug.")
        total = 0
        for school in schools:
            if not (has_module(school, "homework") and school.notify_homework_sms):
                continue
            if opts["dry_run"]:
                self.stdout.write(f"{school}: would send the digest")
                continue
            count = notify_homework_digest(school, timezone.now())
            total += count
            self.stdout.write(f"{school}: {count} message(s) queued")
        self.stdout.write(f"{total} message(s) queued in all.")
