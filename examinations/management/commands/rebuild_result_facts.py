"""
Write the analytics tables again from the published results.

Publishing keeps them up to date on its own; run this once after upgrading, for exams
published before the tables existed, or with --exam to rebuild one exam.
"""

from django.core.management.base import BaseCommand

from examinations.facts import rebuild
from examinations.models import Exam


class Command(BaseCommand):
    help = "Rebuild the analytics tables from each published exam's current snapshots."

    def add_arguments(self, parser):
        parser.add_argument("--exam", type=int, help="One exam's id; every published exam if left out.")

    def handle(self, *args, **options):
        exams = Exam.objects.filter(status="published").select_related("school").order_by("school_id", "pk")
        if options["exam"]:
            exams = Exam.objects.filter(pk=options["exam"])
        total = 0
        for exam in exams:
            written = rebuild(exam)
            total += written
            self.stdout.write(f"  {exam.school}: {exam} ({exam.academic_year}) - {written} result(s)")
        self.stdout.write(self.style.SUCCESS(f"Rebuilt {total} result(s) across {len(exams)} exam(s)."))
