"""
Check a school's online payment gateway before families use it, and find payments that need
attention.

    python manage.py check_gateway                 # every school with SSLCommerz switched on
    python manage.py check_gateway --school demo   # one school
    python manage.py check_gateway --settle        # also recover payments whose confirmation went missing

The store login is tried against SSLCommerz's transaction query API with a made-up transaction
ID, so nothing is charged and nothing is recorded. Run it after entering the sandbox details,
again after switching to the live store, and on a schedule with --settle so a payment whose
return and notification were both lost still gets its receipt.
"""

import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import School
from fees import online
from fees.models import OnlinePayment

# A payment still "started" after this long has most likely been abandoned, or paid with its
# confirmation lost on the way back.
QUIET_MINUTES = 30

LOGIN = {
    "DONE": "store login accepted",
    "FAILED": "store ID or password refused",
    "INACTIVE": "store is inactive; ask SSLCommerz to activate it",
    "INVALID_REQUEST": "request refused as invalid",
}


class Command(BaseCommand):
    help = "Try each school's SSLCommerz store login and list online payments that need attention."

    def add_arguments(self, parser):
        parser.add_argument("--school", help="The school's slug; every school with a gateway if left out.")
        parser.add_argument(
            "--settle",
            action="store_true",
            help=f"Ask the gateway about payments still open after {QUIET_MINUTES} minutes and record any it confirms.",
        )

    def handle(self, *args, **options):
        schools = School.objects.filter(is_active=True).exclude(payment_gateway="none").order_by("name")
        if options["school"]:
            schools = schools.filter(slug=options["school"])
            if not schools:
                raise CommandError(f"No active school '{options['school']}' has online payment switched on.")
        problems = []
        for school in schools:
            self.stdout.write(self.style.MIGRATE_HEADING(school.name))
            if school.payment_gateway == "demo":
                allowed = "allowed on this server" if online.demo_allowed() else "switched off on this server"
                self.stdout.write(f"  Demonstration gateway, {allowed}. No money moves.")
            elif not self.check_store(school):
                problems.append(school.name)
                continue
            self.report_open(school, settle=options["settle"] and school.payment_gateway == "sslcommerz")
        if problems:
            raise CommandError("The gateway is not ready for: " + ", ".join(problems))

    def check_store(self, school):
        mode = "sandbox (test money)" if school.sslcommerz_sandbox else "LIVE (real money)"
        self.stdout.write(f"  SSLCommerz, {mode}, store ID {school.sslcommerz_store_id or '(not set)'}")
        if not (school.sslcommerz_store_id and school.sslcommerz_store_password):
            self.stdout.write(self.style.ERROR("  The store ID and password are not both set."))
            return False
        try:
            reply = online.query_transaction(school, f"CHECK-{uuid.uuid4().hex[:12].upper()}")
        except (OSError, ValueError) as exc:
            self.stdout.write(self.style.ERROR(f"  Could not reach SSLCommerz: {exc}"))
            return False
        status = str(reply.get("APIConnect", "")).upper()
        if status != "DONE":
            self.stdout.write(self.style.ERROR(f"  {LOGIN.get(status, f'unexpected reply {status or reply}')}."))
            return False
        self.stdout.write(self.style.SUCCESS(f"  {LOGIN['DONE']}."))
        return True

    def report_open(self, school, settle):
        cutoff = timezone.now() - timedelta(minutes=QUIET_MINUTES)
        quiet = list(
            OnlinePayment.objects.filter(school=school, status=OnlinePayment.Status.STARTED, created_at__lt=cutoff)
            .select_related("school", "invoice")
            .order_by("created_at")
        )
        review = OnlinePayment.objects.filter(school=school, status=OnlinePayment.Status.REVIEW).count()
        if review:
            self.stdout.write(
                self.style.WARNING(f"  {review} payment(s) taken but held for review; see Fees > Online payments.")
            )
        if not quiet:
            self.stdout.write("  No payments left open.")
            return
        self.stdout.write(f"  {len(quiet)} payment(s) still open after {QUIET_MINUTES} minutes.")
        if not settle:
            self.stdout.write("  Run again with --settle to ask the gateway about them.")
            return
        for attempt in quiet:
            try:
                result = online.recover(attempt)
            except (OSError, ValueError) as exc:
                self.stdout.write(self.style.ERROR(f"  {attempt.tran_id}: could not ask the gateway ({exc})"))
                continue
            if result is None:
                self.stdout.write(f"  {attempt.tran_id}: the gateway holds no payment; left as it is.")
            elif result.status == OnlinePayment.Status.PAID:
                self.stdout.write(
                    self.style.SUCCESS(f"  {attempt.tran_id}: confirmed and recorded as {result.payment.receipt_no}.")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"  {attempt.tran_id}: {result.get_status_display()}, {result.note}")
                )
