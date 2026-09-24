"""
Clear applications once the school's keeping period is over.

Run it nightly, next to the backup. It acts for every school, including one whose admissions
module has since been switched off: clearing a child's old details is always allowed.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from admissions.privacy import purge
from core.models import School


class Command(BaseCommand):
    help = "Clear applications past each school's keeping period."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count the applications without clearing them.")

    def handle(self, *args, **opts):
        today = timezone.localdate()
        total = 0
        for school in School.objects.all():
            count = purge(school, today, dry_run=opts["dry_run"])
            if count:
                verb = "would clear" if opts["dry_run"] else "cleared"
                self.stdout.write(f"{school}: {verb} {count} application(s)")
            total += count
        self.stdout.write(f"{total} application(s) in all.")
