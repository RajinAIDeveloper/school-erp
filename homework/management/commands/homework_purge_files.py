"""
Delete handed-in homework files once the school's keeping period is over.

Run it nightly, next to the backup. It acts for every school, including one whose homework
module has since been switched off: deleting a child's old files is always allowed.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import School
from homework.privacy import purge_files


class Command(BaseCommand):
    help = "Delete handed-in homework files past each school's keeping period."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count the files without deleting them.")

    def handle(self, *args, **opts):
        today = timezone.localdate()
        total = 0
        for school in School.objects.all():
            count = purge_files(school, today, dry_run=opts["dry_run"])
            if count:
                verb = "would delete" if opts["dry_run"] else "deleted"
                self.stdout.write(f"{school}: {verb} {count} file(s)")
            total += count
        self.stdout.write(f"{total} file(s) in all.")
