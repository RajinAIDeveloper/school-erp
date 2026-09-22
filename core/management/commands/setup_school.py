"""
Give a school the defaults it cannot work without.

A new school opens to empty dropdowns: no fee heads, no grading, no periods, no leave
types. This command fills them in with sensible Bangladeshi defaults so the first day is
spent entering students rather than scaffolding. It is safe to run again: everything is
created only when missing, and nothing already entered is overwritten.
"""

from datetime import date, time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from academics.models import AcademicYear
from attendance.models import LeaveType
from core.models import School
from downloads.models import DownloadCategory
from examinations.models import ensure_default_grade_scale
from fees.models import FeeCategory
from finance.models import Account, ensure_default_accounts
from messaging.notifications import ensure_default_templates
from timetable.models import Period, Room

# Fee heads mapped to the income account each one is collected into.
FEE_CATEGORIES = [
    ("Tuition Fee", "4010"),
    ("Admission Fee", "4020"),
    ("Exam Fee", "4030"),
    ("Transport Fee", "4040"),
    ("Other Fee", "4090"),
]

LEAVE_TYPES = [("Casual", 10), ("Sick", 14), ("Earned", 20), ("Unpaid", 0)]

PERIODS = [
    ("1st period", 1, time(9, 0), time(9, 45), False),
    ("2nd period", 2, time(9, 45), time(10, 30), False),
    ("3rd period", 3, time(10, 30), time(11, 15), False),
    ("Tiffin", 4, time(11, 15), time(11, 45), True),
    ("4th period", 5, time(11, 45), time(12, 30), False),
    ("5th period", 6, time(12, 30), time(13, 15), False),
    ("6th period", 7, time(13, 15), time(14, 0), False),
]

DOWNLOAD_CATEGORIES = ["Notices", "Syllabus", "Assignments", "Forms", "Routines"]


def setup_school(school, *, with_year=True):
    """Create whatever is missing. Returns a summary of what was added."""
    added = {}

    ensure_default_accounts(school)
    added["accounts"] = Account.objects.filter(school=school).count()

    created_categories = []
    for name, code in FEE_CATEGORIES:
        income = Account.objects.filter(school=school, code=code).first()
        category, made = FeeCategory.objects.get_or_create(
            school=school, name=name, defaults={"income_account": income}
        )
        if made:
            created_categories.append(category)
        elif category.income_account_id is None and income:
            category.income_account = income
            category.save(update_fields=["income_account", "updated_at"])
    added["fee_categories"] = len(created_categories)

    ensure_default_grade_scale(school)
    added["grade_scale"] = 1

    added["leave_types"] = sum(
        LeaveType.objects.get_or_create(school=school, name=name, defaults={"days_per_year": days})[1]
        for name, days in LEAVE_TYPES
    )
    added["periods"] = sum(
        Period.objects.get_or_create(
            school=school,
            order=order,
            defaults={"name": name, "start_time": start, "end_time": end, "is_break": is_break},
        )[1]
        for name, order, start, end, is_break in PERIODS
    )
    added["rooms"] = sum(
        Room.objects.get_or_create(school=school, name=f"Room {n}", defaults={"capacity": 40})[1] for n in range(1, 4)
    )
    added["download_categories"] = sum(
        DownloadCategory.objects.get_or_create(school=school, name=name)[1] for name in DOWNLOAD_CATEGORIES
    )
    added["sms_templates"] = len(ensure_default_templates(school))

    if with_year and not AcademicYear.objects.filter(school=school).exists():
        year = date.today().year
        AcademicYear.objects.create(
            school=school,
            name=str(year),
            start_date=date(year, 1, 1),
            end_date=date(year, 12, 31),
            is_current=True,
        )
        added["academic_year"] = 1
    return added


class Command(BaseCommand):
    help = "Create the default accounts, fee heads, grading, periods, rooms and templates for a school."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="School slug. Omit it when there is only one school.")

    @transaction.atomic
    def handle(self, *args, **options):
        if options["slug"]:
            school = School.objects.filter(slug=options["slug"]).first()
            if school is None:
                raise CommandError(f"No school with slug {options['slug']!r}.")
        else:
            schools = list(School.objects.all()[:2])
            if not schools:
                raise CommandError("No school exists yet. Create one first, or run seed_demo.")
            if len(schools) > 1:
                raise CommandError("More than one school exists; pass --slug to say which.")
            school = schools[0]

        added = setup_school(school)
        for key, value in added.items():
            self.stdout.write(f"  {key}: {value}")
        self.stdout.write(self.style.SUCCESS(f"{school.name} is ready to use."))
