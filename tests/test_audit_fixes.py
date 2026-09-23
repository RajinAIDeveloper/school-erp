"""
Regressions for the defects a re-audit of this system turned up.

Each test here names a way the software was wrong rather than a feature it has, because
these are the cases that looked fine on screen and were not: a leave approval quietly
rewriting a register, a closed month that still accepted money, a preview that promised
something the import then did differently.
"""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client

from academics.models import ClassLevel, Section
from attendance.models import AttendanceStatus, LeaveRequest, LeaveType, StaffAttendance
from attendance.services import apply_leave, leave_days, withdraw_leave
from core.models import AuditLog
from fees.models import FeeInvoice, FeePayment
from fees.services import cancel_payment, collect_payment, create_invoice, edit_invoice, sibling_discount_for
from finance.models import Account, JournalEntry, record_simple_entry
from finance.services import close_books, pay_payroll, post_journal, reverse_payroll
from students.models import Enrollment, Guardian, Student, StudentGuardian
from students.services import import_students, validate_import

MONDAY = date(2026, 9, 21)


def account(erp, code):
    return Account.objects.get(school=erp.school, code=code)


def leave_type(erp, name="Casual", days=10, allow_negative=False):
    return LeaveType.objects.create(school=erp.school, name=name, days_per_year=days, allow_negative=allow_negative)


# ------------------------------------------------------- leave must not destroy a register


def test_approving_leave_never_overwrites_an_attendance_row_already_recorded(erp):
    """
    Someone recorded that this person was in. An approval must not rewrite that.

    The old code used update_or_create, so approving leave across a day already marked
    "present" took ownership of that row, changed it to leave, and then deleted it
    outright when the approval was withdrawn.
    """
    kind = leave_type(erp)
    manual = StaffAttendance.objects.create(
        school=erp.school,
        employee=erp.employee,
        date=MONDAY,
        status=AttendanceStatus.PRESENT,
        remarks="Came in for the exam",
    )
    request = LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=MONDAY,
        end_date=MONDAY + timedelta(days=2),
    )
    outcome = apply_leave(request, erp.admin)

    manual.refresh_from_db()
    assert manual.status == AttendanceStatus.PRESENT
    assert manual.remarks == "Came in for the exam"
    assert manual.created_by_leave_id is None
    assert outcome.created == 2  # the other two working days
    assert outcome.skipped == [MONDAY]
    assert "already had an attendance entry" in outcome.summary


def test_withdrawing_leave_deletes_only_what_the_approval_created(erp):
    kind = leave_type(erp)
    manual = StaffAttendance.objects.create(
        school=erp.school, employee=erp.employee, date=MONDAY, status=AttendanceStatus.PRESENT
    )
    request = LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=MONDAY,
        end_date=MONDAY + timedelta(days=2),
    )
    apply_leave(request, erp.admin)
    withdraw_leave(request, erp.admin)

    assert StaffAttendance.objects.filter(pk=manual.pk).exists()
    assert not StaffAttendance.objects.filter(created_by_leave=request).exists()


def test_the_approval_screen_reports_the_days_it_could_not_take(erp):
    kind = leave_type(erp)
    StaffAttendance.objects.create(
        school=erp.school, employee=erp.employee, date=MONDAY, status=AttendanceStatus.PRESENT
    )
    request = LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=MONDAY,
        end_date=MONDAY + timedelta(days=1),
    )
    client = Client()
    client.force_login(erp.admin)
    response = client.post(f"/attendance/leave/{request.pk}/review/", {"status": "approved"}, follow=True)
    assert b"already had an attendance entry" in response.content


def test_a_leave_decision_and_its_register_rows_land_together(erp):
    """The approval is atomic: a failure writing the register leaves the request pending."""
    from unittest import mock

    kind = leave_type(erp)
    request = LeaveRequest.objects.create(
        school=erp.school, employee=erp.employee, leave_type=kind, start_date=MONDAY, end_date=MONDAY
    )
    client = Client()
    client.force_login(erp.admin)
    with mock.patch("attendance.views.apply_leave", side_effect=RuntimeError("register unavailable")):
        with pytest.raises(RuntimeError):
            client.post(f"/attendance/leave/{request.pk}/review/", {"status": "approved"})
    request.refresh_from_db()
    assert request.status == "pending"
    assert not StaffAttendance.objects.exists()


# --------------------------------------------------------------- leave counted in working days


def test_leave_is_counted_in_working_days_everywhere(erp):
    """One definition: the school's open days. Thursday to Sunday is two days here."""
    from holidays.models import Holiday

    kind = leave_type(erp)
    thursday, sunday = date(2026, 9, 24), date(2026, 9, 27)
    request = LeaveRequest.objects.create(
        school=erp.school, employee=erp.employee, leave_type=kind, start_date=thursday, end_date=sunday
    )
    # Friday and Saturday are the weekend for this school.
    assert leave_days(erp.school, thursday, sunday) == 2
    assert request.days == 2

    Holiday.objects.create(school=erp.school, name="Civic day", start_date=sunday, end_date=sunday)
    assert LeaveRequest.objects.get(pk=request.pk).days == 1


def test_leave_across_new_year_is_charged_to_each_year_separately(erp):
    """A request spanning 31 December used to charge every one of its days to the start year."""
    from academics.models import AcademicYear
    from attendance.services import leave_balance

    AcademicYear.objects.create(
        school=erp.school, name="2027", start_date=date(2027, 1, 1), end_date=date(2027, 12, 31)
    )
    kind = leave_type(erp, days=20)
    LeaveRequest.objects.create(
        school=erp.school,
        employee=erp.employee,
        leave_type=kind,
        start_date=date(2026, 12, 28),
        end_date=date(2027, 1, 8),
        status=LeaveRequest.Status.APPROVED,
    )
    used_2026 = next(row["used"] for row in leave_balance(erp.employee, 2026) if row["leave_type"] == kind)
    used_2027 = next(row["used"] for row in leave_balance(erp.employee, 2027) if row["leave_type"] == kind)
    assert used_2026 == leave_days(erp.school, date(2026, 12, 28), date(2026, 12, 31))
    assert used_2027 == leave_days(erp.school, date(2027, 1, 1), date(2027, 1, 8))
    assert used_2026 and used_2027


def test_a_leave_type_may_be_allowed_to_go_negative(erp):
    from attendance.views import LeaveForm

    strict = leave_type(erp, name="Casual", days=1)
    lenient = leave_type(erp, name="Unpaid", days=1, allow_negative=True)
    payload = {
        "employee": erp.employee.pk,
        "start_date": "2026-09-21",
        "end_date": "2026-09-24",
        "reason": "Family matter",
    }
    assert not LeaveForm({**payload, "leave_type": strict.pk}, school=erp.school).is_valid()
    assert LeaveForm({**payload, "leave_type": lenient.pk}, school=erp.school).is_valid()


def test_a_request_covering_only_closed_days_is_refused(erp):
    from attendance.views import LeaveForm

    kind = leave_type(erp)
    # 25 and 26 September 2026 are the Friday and Saturday weekend.
    form = LeaveForm(
        {
            "employee": erp.employee.pk,
            "leave_type": kind.pk,
            "start_date": "2026-09-25",
            "end_date": "2026-09-26",
            "reason": "",
        },
        school=erp.school,
    )
    assert not form.is_valid()
    assert "no working days" in " ".join(form.errors["start_date"])


def test_the_leave_list_filters_by_employee_and_month(erp):
    from employees.models import Employee

    kind = leave_type(erp)
    colleague = Employee.objects.create(
        school=erp.school,
        employee_id="S77",
        first_name="Colleague",
        gender="M",
        phone="01712345677",
        joining_date=date(2026, 1, 1),
    )
    LeaveRequest.objects.create(
        school=erp.school, employee=erp.employee, leave_type=kind, start_date=MONDAY, end_date=MONDAY
    )
    LeaveRequest.objects.create(
        school=erp.school,
        employee=colleague,
        leave_type=kind,
        start_date=date(2026, 11, 2),
        end_date=date(2026, 11, 3),
    )
    client = Client()
    client.force_login(erp.admin)

    def listed(query):
        # The rows themselves; the employee picker naturally names everyone.
        return {row.employee_id for row in client.get(f"/attendance/leave/{query}").context["rows"]}

    assert listed("") == {erp.employee.pk, colleague.pk}
    assert listed(f"?employee={erp.employee.pk}") == {erp.employee.pk}
    assert listed(f"?employee={colleague.pk}") == {colleague.pk}
    assert listed("?month=2026-09") == {erp.employee.pk}
    assert listed("?month=2026-11") == {colleague.pk}


# ------------------------------------------------------------------ the period lock holds


def test_a_closed_period_refuses_a_fee_cancellation_and_leaves_the_receipt_alone(erp, invoice):
    """
    Cancelling used to bypass the lock entirely, and mark the receipt cancelled even when
    the reversal was refused, leaving a receipt with no matching ledger entry.
    """
    payment = collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=Decimal("500"),
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    close_books(school=erp.school, user=erp.admin, through=date(2026, 9, 30))
    erp.school.refresh_from_db()

    with pytest.raises(ValidationError):
        cancel_payment(school=erp.school, user=erp.accountant, payment=payment, reason="Cheque bounced")

    payment.refresh_from_db()
    assert not payment.is_cancelled
    assert payment.cancel_reason == ""
    assert payment.journal_entry.status == "posted"
    assert JournalEntry.objects.filter(source="reversal").count() == 0
    invoice.refresh_from_db()
    assert invoice.paid == Decimal("500.00")


def test_a_closed_period_refuses_a_journal_reversal(erp):
    entry = post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 1),
        narration="Rent",
        lines=[(account(erp, "5020"), 500, 0), (account(erp, "1010"), 0, 500)],
    )
    close_books(school=erp.school, user=erp.admin, through=date(2030, 12, 31))
    erp.school.refresh_from_db()
    entry.school.refresh_from_db()
    with pytest.raises(ValidationError):
        entry.reverse(user=erp.admin)
    entry.refresh_from_db()
    assert entry.status == "posted"


def test_a_closed_period_refuses_a_simple_entry(erp):
    close_books(school=erp.school, user=erp.admin, through=date(2026, 9, 30))
    erp.school.refresh_from_db()
    with pytest.raises(ValidationError):
        record_simple_entry(
            erp.school,
            date(2026, 9, 5),
            "Backdated stationery",
            account(erp, "5040"),
            account(erp, "1010"),
            Decimal("100"),
            source=JournalEntry.Source.EXPENSE,
        )
    assert not JournalEntry.objects.exists()


def test_a_closed_period_refuses_a_salary_payment(erp):
    from finance.models import Payroll

    row = Payroll.objects.create(
        school=erp.school, employee=erp.employee, month=date(2026, 9, 1), basic=Decimal("1000")
    )
    close_books(school=erp.school, user=erp.admin, through=date(2030, 12, 31))
    erp.school.refresh_from_db()
    with pytest.raises(ValidationError):
        pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="cash", paid_on=date(2026, 9, 28))
    row.refresh_from_db()
    assert row.journal_entry_id is None and row.paid_on is None


def test_opening_balances_refuse_a_closed_period_and_parse_their_date(erp):
    close_books(school=erp.school, user=erp.admin, through=date(2026, 12, 31))
    erp.school.refresh_from_db()
    client = Client()
    client.force_login(erp.accountant)
    posted = client.post(
        "/finance/opening-balances/",
        {"date": "2026-01-01", f"amount-{account(erp, '1010').pk}": "5000"},
        follow=True,
    )
    assert b"books are closed" in posted.content
    assert not JournalEntry.objects.exists()

    # A date that is not a date is a form error, never a comparison between a string and
    # a date deeper in the service.
    nonsense = client.post(
        "/finance/opening-balances/",
        {"date": "not-a-date", f"amount-{account(erp, '1010').pk}": "5000"},
        follow=True,
    )
    assert nonsense.status_code == 200
    assert not JournalEntry.objects.exists()


def test_opening_balances_post_when_the_period_is_open(erp):
    client = Client()
    client.force_login(erp.accountant)
    response = client.post(
        "/finance/opening-balances/",
        {"date": "2026-01-01", f"amount-{account(erp, '1010').pk}": "5000"},
        follow=True,
    )
    assert response.status_code == 200
    entry = JournalEntry.objects.get()
    assert entry.date == date(2026, 1, 1)
    assert account(erp, "1010").balance() == Decimal("5000")


# ------------------------------------------------------------------ payroll can be undone


def test_a_salary_payment_can_be_reversed_from_payroll_and_paid_again(erp):
    """
    The ledger refuses to reverse a salary entry and says to do it from payroll. Until
    now, payroll had no way to do it.
    """
    from finance.models import Payroll

    row = Payroll.objects.create(
        school=erp.school, employee=erp.employee, month=date(2026, 9, 1), basic=Decimal("1000")
    )
    pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="cash", paid_on=date(2026, 9, 28))
    assert account(erp, "5010").balance() == Decimal("1000")

    reverse_payroll(school=erp.school, user=erp.accountant, payroll=row, reason="Paid the wrong person")
    row.refresh_from_db()
    assert row.journal_entry_id is None and row.paid_on is None
    # History is preserved: both entries stay, and they net to nothing.
    assert JournalEntry.objects.count() == 2
    assert account(erp, "5010").balance() == Decimal("0")
    assert account(erp, "1010").balance() == Decimal("0")
    assert AuditLog.objects.filter(action="payroll.reversed").exists()

    # The correction can then be made.
    pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="bank", paid_on=date(2026, 9, 29))
    row.refresh_from_db()
    assert row.paid_on == date(2026, 9, 29)
    assert account(erp, "1020").balance() == Decimal("-1000")


def test_reversing_a_salary_needs_a_reason_and_something_to_reverse(erp):
    from finance.models import Payroll

    row = Payroll.objects.create(
        school=erp.school, employee=erp.employee, month=date(2026, 9, 1), basic=Decimal("1000")
    )
    with pytest.raises(ValidationError):
        reverse_payroll(school=erp.school, user=erp.accountant, payroll=row, reason="Wrong")

    pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="cash")
    with pytest.raises(ValidationError):
        reverse_payroll(school=erp.school, user=erp.accountant, payroll=row, reason="   ")


def test_reversing_a_salary_into_a_closed_period_is_refused(erp):
    from finance.models import Payroll

    row = Payroll.objects.create(
        school=erp.school, employee=erp.employee, month=date(2026, 9, 1), basic=Decimal("1000")
    )
    pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="cash", paid_on=date(2026, 9, 28))
    close_books(school=erp.school, user=erp.admin, through=date(2030, 12, 31))
    erp.school.refresh_from_db()
    with pytest.raises(ValidationError):
        reverse_payroll(school=erp.school, user=erp.accountant, payroll=row, reason="Paid twice")
    row.refresh_from_db()
    assert row.journal_entry_id is not None


def test_payroll_reversal_is_for_payroll_staff_only(erp):
    from finance.models import Payroll

    row = Payroll.objects.create(
        school=erp.school, employee=erp.employee, month=date(2026, 9, 1), basic=Decimal("1000")
    )
    pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="cash")
    with pytest.raises(PermissionDenied):
        reverse_payroll(school=erp.school, user=erp.teacher, payroll=row, reason="Not mine to undo")


# ------------------------------------------------------------------------ invoices


def test_an_unpaid_invoice_can_be_corrected_and_the_change_is_audited(erp, invoice):
    from fees.models import FeeCategory

    transport = FeeCategory.objects.create(school=erp.school, name="Transport")
    edit_invoice(
        school=erp.school,
        user=erp.admin,
        invoice=invoice,
        items=[(erp.category, Decimal("900"), "Tuition, September"), (transport, Decimal("200"), "Bus")],
        discount=Decimal("100"),
        notes="Corrected after the fee committee met",
    )
    invoice.refresh_from_db()
    assert invoice.total == Decimal("1000.00")
    assert invoice.items.count() == 2
    assert AuditLog.objects.filter(action="invoice.edited").exists()


def test_an_invoice_with_money_against_it_cannot_be_edited(erp, invoice):
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=Decimal("100"),
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    with pytest.raises(ValidationError):
        edit_invoice(school=erp.school, user=erp.admin, invoice=invoice, items=[(erp.category, Decimal("50"), "")])
    invoice.refresh_from_db()
    assert invoice.subtotal == Decimal("1000.00")


def test_the_edit_screen_is_offered_only_while_editing_is_possible(erp, invoice):
    client = Client()
    client.force_login(erp.admin)
    assert b"Edit invoice" in client.get(f"/fees/{invoice.pk}/").content
    assert client.get(f"/fees/{invoice.pk}/edit/").status_code == 200

    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=Decimal("100"),
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    assert b"Edit invoice" not in client.get(f"/fees/{invoice.pk}/").content
    assert client.get(f"/fees/{invoice.pk}/edit/").status_code == 302


def test_an_invoice_line_description_is_saved_not_dropped(erp):
    """The form asked for it, the screen showed a column for it, and nothing stored it."""
    from fees.models import FeeCategory

    category = FeeCategory.objects.create(school=erp.school, name="Transport")
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/fees/new/",
        {
            "student": erp.student.pk,
            "issue_date": "2026-09-01",
            "due_date": "2026-09-20",
            "month": "",
            "discount": "0",
            "late_fee": "0",
            "notes": "",
            "items-TOTAL_FORMS": "4",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "20",
            "items-0-category": category.pk,
            "items-0-description": "Dhanmondi route, one term",
            "items-0-amount": "600",
            **{f"items-{n}-{f}": "" for n in (1, 2, 3) for f in ("category", "description", "amount")},
        },
    )
    assert response.status_code == 302
    assert FeeInvoice.objects.get().items.get().description == "Dhanmondi route, one term"


def test_a_discount_larger_than_the_fees_is_refused(erp):
    with pytest.raises(ValidationError, match="more than the fees charged"):
        create_invoice(
            school=erp.school,
            user=erp.admin,
            student=erp.student,
            enrollment=erp.enrollment,
            academic_year=erp.year,
            month=None,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 9, 20),
            items=[(erp.category, Decimal("500"))],
            discount=Decimal("600"),
        )
    assert not FeeInvoice.objects.exists()


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity"])
def test_a_fee_line_that_is_not_real_money_is_refused(erp, bad):
    with pytest.raises(ValidationError):
        create_invoice(
            school=erp.school,
            user=erp.admin,
            student=erp.student,
            enrollment=erp.enrollment,
            academic_year=erp.year,
            month=None,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 9, 20),
            items=[(erp.category, Decimal(bad))],
        )
    assert not FeeInvoice.objects.exists()


@pytest.mark.parametrize("field", ["discount", "late_fee"])
def test_a_negative_adjustment_is_refused(erp, field):
    with pytest.raises(ValidationError):
        create_invoice(
            school=erp.school,
            user=erp.admin,
            student=erp.student,
            enrollment=erp.enrollment,
            academic_year=erp.year,
            month=None,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 9, 20),
            items=[(erp.category, Decimal("500"))],
            **{field: Decimal("-10")},
        )
    assert not FeeInvoice.objects.exists()


# -------------------------------------------------------- the import preview tells the truth


def csv_upload(body):
    header = (
        "student_id,first_name,gender,date_of_birth,admission_date,"
        "guardian_name,guardian_phone,guardian_relation,class_level,section,roll_number\n"
    )
    return SimpleUploadedFile("students.csv", (header + body).encode())


def rows_from(body):
    import csv
    from io import StringIO

    header = (
        "student_id,first_name,gender,date_of_birth,admission_date,"
        "guardian_name,guardian_phone,guardian_relation,class_level,section,roll_number\n"
    )
    return list(csv.DictReader(StringIO(header + body)))


@pytest.mark.parametrize(
    "row,expected",
    [
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,cousin,Class 1,A,5", "is not a guardian relation"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,12345,father,Class 1,A,5", "not a Bangladeshi mobile"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,,01712345611,father,Class 1,A,5", "both a name and a mobile"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 9,A,5", "no class called"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,Z,5", "has no section"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,A,five", "is not a number"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,A,0", "outside 1 to"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,A,1", "already taken this year"),
        ("I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,,5", "both a class and a section"),
    ],
)
def test_the_preview_catches_what_the_import_would_have_hit(erp, row, expected):
    """Every one of these used to pass the preview and then misbehave on confirmation."""
    _prepared, errors = validate_import(erp.school, rows_from(row + "\n"))
    assert any(expected in error for error in errors), errors
    with pytest.raises(ValidationError):
        import_students(erp.school, csv_upload(row + "\n"))
    assert not Student.objects.filter(student_id="I1").exists()


def test_two_rows_claiming_the_same_roll_are_caught_before_anything_is_written(erp):
    body = (
        "I1,One,M,2016-01-01,2026-01-01,Parent A,01712345611,father,Class 1,A,7\n"
        "I2,Two,F,2016-01-01,2026-01-01,Parent B,01712345612,mother,Class 1,A,7\n"
    )
    _prepared, errors = validate_import(erp.school, rows_from(body))
    assert any("already used by row" in error for error in errors)
    with pytest.raises(ValidationError):
        import_students(erp.school, csv_upload(body))
    assert Student.objects.count() == 1  # only the fixture's student


def test_a_supplied_roll_is_never_quietly_replaced(erp):
    """It used to fall back to the next free roll, so the sheet and the system disagreed."""
    body = "I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,A,12\n"
    import_students(erp.school, csv_upload(body))
    assert Enrollment.objects.get(student__student_id="I1").roll_number == 12


def test_a_blank_roll_is_allocated_without_colliding_inside_the_file(erp):
    body = (
        "I1,One,M,2016-01-01,2026-01-01,Parent A,01712345611,father,Class 1,A,\n"
        "I2,Two,F,2016-01-01,2026-01-01,Parent B,01712345612,mother,Class 1,A,\n"
    )
    import_students(erp.school, csv_upload(body))
    rolls = sorted(Enrollment.objects.filter(section=erp.section).values_list("roll_number", flat=True))
    assert rolls == [1, 2, 3]


def test_a_file_that_places_students_needs_a_current_year(erp):
    erp.year.is_current = False
    erp.year.save()
    body = "I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,A,5\n"
    _prepared, errors = validate_import(erp.school, rows_from(body))
    assert any("no current academic year" in error for error in errors)


def test_a_good_file_still_imports_everything_the_preview_promised(erp):
    body = "I1,Rafi,M,2016-01-01,2026-01-01,Parent,01712345611,father,Class 1,A,9\n"
    prepared, errors = validate_import(erp.school, rows_from(body))
    assert not errors and prepared[0]["valid"]
    assert import_students(erp.school, rows_from(body)) == 1
    student = Student.objects.get(student_id="I1")
    assert student.primary_guardian.phone == "01712345611"
    assert student.current_enrollment.roll_number == 9
    assert student.guardian_links.get().relation == "father"


# ------------------------------------------------------------- one family, one phone, one primary


def test_a_guardian_written_two_ways_is_still_one_family(erp):
    """017... and +88017... used to create two guardian records, and so two families."""
    from students.services import find_guardian

    existing = Guardian.objects.create(school=erp.school, full_name="Shared", phone="+8801712345650")
    assert existing.phone == "01712345650"
    assert find_guardian(erp.school, "01712345650") == existing
    assert find_guardian(erp.school, "8801712345650") == existing
    assert find_guardian(erp.school, "017-1234 5650") == existing


def test_admission_reuses_a_guardian_whatever_form_the_number_was_written_in(erp):
    Guardian.objects.create(school=erp.school, full_name="Kamal", phone="+8801712345699")
    client = Client()
    client.force_login(erp.admin)
    client.post(
        "/students/admit/",
        {
            "student_id": "S-PHONE-1",
            "first_name": "Nusrat",
            "gender": "F",
            "date_of_birth": "2016-04-02",
            "admission_date": "2026-01-05",
            "guardian_name": "Kamal Hossain",
            "guardian_phone": "01712345699",
            "guardian_relation": "father",
            "academic_year": erp.year.pk,
            "class_level": erp.level.pk,
            "section": erp.section.pk,
        },
    )
    assert Guardian.objects.filter(school=erp.school, phone="01712345699").count() == 1


def test_a_child_cannot_have_two_primary_guardians(erp):
    second = Guardian.objects.create(school=erp.school, full_name="Second", phone="01712345651")
    StudentGuardian.objects.create(student=erp.student, guardian=second, relation="mother", is_primary=True)
    # Promoting one stands the other down rather than failing.
    assert StudentGuardian.objects.filter(student=erp.student, is_primary=True).count() == 1
    assert StudentGuardian.objects.get(student=erp.student, is_primary=True).guardian == second


def test_the_database_itself_refuses_a_second_primary_guardian(erp):
    """The rule holds even against a write that goes round the model."""
    second = Guardian.objects.create(school=erp.school, full_name="Second", phone="01712345652")
    link = StudentGuardian.objects.create(student=erp.student, guardian=second, relation="mother")
    with pytest.raises(IntegrityError), transaction.atomic():
        StudentGuardian.objects.filter(pk=link.pk).update(is_primary=True)


# ------------------------------------------------------------------ the sibling rate is a percentage


@pytest.mark.parametrize("rate", [Decimal("0"), Decimal("100")])
def test_the_sibling_discount_accepts_its_boundaries(erp, rate):
    erp.school.sibling_discount_percent = rate
    erp.school.full_clean()
    erp.school.save()


@pytest.mark.parametrize("rate", [Decimal("-1"), Decimal("101")])
def test_the_sibling_discount_refuses_anything_that_is_not_a_percentage(erp, rate):
    erp.school.sibling_discount_percent = rate
    with pytest.raises(ValidationError):
        erp.school.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        erp.school.save()


def test_a_hundred_percent_sibling_discount_makes_a_free_invoice_not_a_negative_one(erp):
    from fees.services import generate_invoices

    younger = Student.objects.create(
        school=erp.school,
        student_id="FREE-1",
        first_name="Younger",
        gender="M",
        date_of_birth=date(2018, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school,
        student=younger,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    StudentGuardian.objects.create(student=younger, guardian=erp.guardian, relation="mother", is_primary=True)
    erp.school.sibling_discount_percent = Decimal("100")
    erp.school.save()
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    totals = {inv.student.first_name: inv.total for inv in FeeInvoice.objects.select_related("student")}
    assert totals["Ayesha"] == Decimal("1000.00")
    assert totals["Younger"] == Decimal("0.00")
    assert all(total >= 0 for total in totals.values())


def test_the_policy_form_refuses_a_discount_above_a_hundred(erp):
    from core.forms import FinancePolicyForm

    form = FinancePolicyForm({"late_fee_per_day": "0", "late_fee_cap": "0", "sibling_discount_percent": "150"})
    assert not form.is_valid()
    assert "sibling_discount_percent" in form.errors


def test_an_out_of_range_percentage_cannot_even_be_written(erp):
    """
    The database itself refuses it, so the service can never read one back.

    Belt and braces: the guard in fees.services stands whatever else writes the row, and
    this constraint means nothing ever gets that far.
    """
    from core.models import School

    with pytest.raises(IntegrityError), transaction.atomic():
        School.objects.filter(pk=erp.school.pk).update(sibling_discount_percent=Decimal("150"))
    erp.school.refresh_from_db()
    assert erp.school.sibling_discount_percent == Decimal("0")
    assert sibling_discount_for(erp.school, erp.student) == Decimal("0")


# ---------------------------------------------------------------------- scoping


def test_a_teacher_sees_only_this_year_s_roll_of_the_sections_they_teach(erp):
    """A former pupil of a section taught three years ago is not this teacher's business."""
    from academics.models import AcademicYear
    from core.access import students_for

    old_year = AcademicYear.objects.create(
        school=erp.school, name="2023", start_date=date(2023, 1, 1), end_date=date(2023, 12, 31)
    )
    alumnus = Student.objects.create(
        school=erp.school,
        student_id="OLD-1",
        first_name="Alumnus",
        gender="M",
        date_of_birth=date(2010, 1, 1),
        admission_date=date(2023, 1, 1),
        status=Student.Status.GRADUATED,
    )
    Enrollment.objects.create(
        school=erp.school,
        student=alumnus,
        academic_year=old_year,
        class_level=erp.level,
        section=erp.section,
        roll_number=40,
        status=Enrollment.Status.LEFT,
    )
    visible = set(students_for(erp.teacher, erp.school).values_list("student_id", flat=True))
    assert visible == {"S1"}

    client = Client()
    client.force_login(erp.teacher)
    assert client.get(f"/students/{alumnus.pk}/").status_code == 404


def test_a_withdrawn_student_drops_off_a_teacher_s_roster(erp):
    from core.access import students_for

    erp.student.status = Student.Status.WITHDRAWN
    erp.student.save()
    assert not students_for(erp.teacher, erp.school).exists()


def test_a_past_teaching_assignment_does_not_grant_this_year_s_class(erp):
    from academics.models import AcademicYear, SubjectTeacher
    from core.access import sections_for

    last_year = AcademicYear.objects.create(
        school=erp.school, name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    SubjectTeacher.objects.filter(teacher=erp.employee).update(academic_year=last_year)
    assert not sections_for(erp.teacher, erp.school).exists()
    # The historical assignment still authorises that year's records.
    assert sections_for(erp.teacher, erp.school, last_year).filter(pk=erp.section.pk).exists()


def test_report_cards_for_a_past_exam_use_that_year_s_assignment(erp):
    from academics.models import AcademicYear, SubjectTeacher
    from examinations.models import Exam

    last_year = AcademicYear.objects.create(
        school=erp.school, name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    old_exam = Exam.objects.create(
        school=erp.school, academic_year=last_year, name="Annual 2025", grade_scale=erp.scale
    )
    client = Client()
    client.force_login(erp.teacher)
    # Not assigned to that section in 2025, so no admit cards for it.
    denied = client.get(f"/exams/admit-cards.pdf?exam={old_exam.pk}&section={erp.section.pk}")
    assert denied.status_code == 403

    SubjectTeacher.objects.create(
        school=erp.school,
        teacher=erp.employee,
        section=erp.section,
        subject=erp.subject,
        academic_year=last_year,
    )
    allowed = client.get(f"/exams/admit-cards.pdf?exam={old_exam.pk}&section={erp.section.pk}")
    assert allowed.status_code == 200


def test_a_family_cannot_open_the_planning_reports(erp):
    client = Client()
    client.force_login(erp.parent)
    assert client.get("/reports/teacher-load/").status_code == 403
    assert client.get("/routine/utilisation/").status_code == 403
    assert client.get("/routine/free-teachers.json").status_code == 403


def test_a_plain_teacher_cannot_open_the_planning_reports_either(erp):
    client = Client()
    client.force_login(erp.teacher)
    assert client.get("/reports/teacher-load/").status_code == 403
    assert client.get("/routine/free-teachers.json").status_code == 403
    assert b"Teacher load" not in client.get("/reports/").content


def test_marks_cannot_be_written_for_a_student_of_another_section(erp):
    """A hand-made POST naming someone else's enrollment is refused, not written."""
    from examinations.services import save_marks

    outsider = Student.objects.create(
        school=erp.school,
        student_id="OUT-1",
        first_name="Outsider",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    elsewhere = Enrollment.objects.create(
        school=erp.school,
        student=outsider,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.other_section,
        roll_number=1,
    )
    with pytest.raises(ValidationError, match="not in"):
        save_marks(
            user=erp.admin,
            schedule=erp.schedule,
            section=erp.section,
            rows=[(elsewhere, Decimal("90"), False, 0)],
        )
    from examinations.models import Mark

    assert not Mark.objects.exists()


# ---------------------------------------------------------------------- timetable


@pytest.fixture
def periods(erp):
    first = Period.objects.create(school=erp.school, name="P1", order=1, start_time=time(9), end_time=time(9, 45))
    overlapping = Period.objects.create(
        school=erp.school, name="P1 long", order=2, start_time=time(9, 30), end_time=time(10, 15)
    )
    return first, overlapping


def test_free_teachers_respects_overlapping_times_not_just_the_same_period(erp, periods):
    """A teacher busy from 9:00 to 9:45 is not free for a lesson starting at 9:30."""
    from timetable.models import RoutineSlot
    from timetable.services import free_teachers

    first, overlapping = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=1,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    free = free_teachers(erp.school, erp.year, 1, overlapping)
    assert erp.employee not in list(free)


def test_free_teachers_answers_for_the_year_being_edited(erp, periods):
    from academics.models import AcademicYear
    from timetable.models import RoutineSlot

    first, _ = periods
    next_year = AcademicYear.objects.create(
        school=erp.school, name="2027", start_date=date(2027, 1, 1), end_date=date(2027, 12, 31)
    )
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=next_year,
        section=erp.section,
        weekday=1,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    client = Client()
    client.force_login(erp.admin)
    # Busy next year, free this year. Asking without a year used to answer for the
    # current one whatever year the editor was actually working in.
    this_year = client.get(f"/routine/free-teachers.json?weekday=1&period={first.pk}").json()
    assert "Teacher" in {row["name"] for row in this_year}
    next_year_rows = client.get(
        f"/routine/free-teachers.json?weekday=1&period={first.pk}&academic_year={next_year.pk}"
    ).json()
    assert "Teacher" not in {row["name"] for row in next_year_rows}


def test_a_tampered_subject_id_refuses_the_grid_instead_of_clearing_a_period(erp, periods):
    from timetable.models import RoutineSlot

    first, _ = periods
    RoutineSlot.objects.create(
        school=erp.school,
        academic_year=erp.year,
        section=erp.section,
        weekday=1,
        period=first,
        subject=erp.subject,
        teacher=erp.employee,
    )
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/routine/edit/",
        {
            "academic_year": erp.year.pk,
            "section": erp.section.pk,
            f"1-{first.pk}-subject": "999999",
            f"1-{first.pk}-teacher": "",
            f"1-{first.pk}-room": "",
        },
    )
    assert response.status_code == 200
    assert b"not available" in response.content
    # The existing lesson is still there: an unreadable value is not an instruction to clear.
    assert RoutineSlot.objects.count() == 1


def test_retired_master_data_is_not_offered_for_a_new_week(erp, periods):
    from academics.models import Subject
    from timetable.models import Room

    retired_subject = Subject.objects.create(school=erp.school, name="Latin", code="LAT", is_active=False)
    retired_room = Room.objects.create(school=erp.school, name="Old hall", is_active=False)
    client = Client()
    client.force_login(erp.admin)
    body = client.get(f"/routine/edit/?academic_year={erp.year.pk}&section={erp.section.pk}").content
    assert retired_subject.name.encode() not in body
    assert retired_room.name.encode() not in body
    assert erp.subject.name.encode() in body


# ---------------------------------------------------------------------- calendar


def test_ical_escapes_a_newline_as_two_characters(erp):
    """A real newline ends the property as far as a parser is concerned."""
    from holidays.models import Holiday

    Holiday.objects.create(
        school=erp.school,
        name="Autumn; break, long",
        description="Closed all week.\nReopens on the Sunday.",
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 2),
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/holidays/calendar.ics").content.decode()
    assert "SUMMARY:Autumn\\; break\\, long" in body
    assert "Closed all week.\\nReopens on the Sunday." in body
    # Every line is a property or a folded continuation; none is a stray fragment.
    for line in body.split("\r\n"):
        assert not line or line.startswith(" ") or ":" in line


def test_ical_folds_a_long_line(erp):
    from holidays.models import Holiday

    Holiday.objects.create(
        school=erp.school,
        name="শরৎকালীন ছুটি " * 8,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 1),
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get("/holidays/calendar.ics").content.decode()
    lines = body.split("\r\n")
    assert all(len(line.encode()) <= 75 for line in lines), [line for line in lines if len(line.encode()) > 75]
    assert any(line.startswith(" ") for line in lines)


def test_the_national_holiday_import_follows_the_notices_for_each_year(erp):
    """
    15 August stopped being a public holiday with the notice of 13 August 2024.

    Importing 2023 still produces the calendar that year was actually kept to; importing
    2026 does not invent a holiday that no longer exists.
    """
    from holidays.models import Holiday
    from holidays.services import import_national_holidays

    import_national_holidays(erp.school, 2023, erp.admin)
    assert Holiday.objects.filter(school=erp.school, name="National Mourning Day").exists()

    Holiday.objects.all().delete()
    for year in (2024, 2026):
        import_national_holidays(erp.school, year, erp.admin)
    assert not Holiday.objects.filter(name="National Mourning Day").exists()
    assert Holiday.objects.filter(name="Victory Day").count() == 2


# ---------------------------------------------------------------------- operations


def test_the_health_probe_says_nothing_useful_to_a_stranger(erp):
    """It has no sign-in, so a driver's exception text is not its to hand out."""
    import json
    from unittest import mock

    leaky = RuntimeError("could not connect: host=db.internal port=5432 user=erp password=hunter2")
    # Patched inside a with-block so the connection is whole again before teardown.
    with mock.patch("django.db.connection.cursor", side_effect=leaky):
        response = Client().get("/healthz/")
    assert response.status_code == 503
    payload = json.loads(response.content)
    assert payload["checks"]["database"] == "failed"
    body = response.content.decode()
    assert "hunter2" not in body and "db.internal" not in body and "5432" not in body


def test_the_backup_command_keeps_the_password_off_the_command_line(erp, settings, tmp_path, monkeypatch):
    """A connection URI puts the password in the process list for every user to read."""
    import subprocess

    from django.core.management import call_command

    settings.MEDIA_ROOT = tmp_path / "media"
    settings.DATABASES = {
        **settings.DATABASES,
        "default": {
            **settings.DATABASES["default"],
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "school_erp",
            "USER": "erp",
            "PASSWORD": "p@ss/word:with#specials",
            "HOST": "db",
            "PORT": 5432,
        },
    }
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs.get("env", {})
        kwargs["stdout"].write(b"-- dump\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    call_command("backup", output=str(tmp_path / "backups"), skip_media=True, verbosity=0)

    assert all("p@ss/word:with#specials" not in str(part) for part in seen["command"])
    assert seen["env"]["PGPASSWORD"] == "p@ss/word:with#specials"
    assert "--no-password" in seen["command"]
    assert "--dbname=school_erp" in seen["command"]


def test_the_access_matrix_on_disk_matches_the_code(erp):
    """
    A generated document that nobody regenerates is just a stale document.

    Run `python manage.py role_matrix` and rewrite docs/access-matrix.md when this fails.
    """
    from io import StringIO
    from pathlib import Path

    from django.core.management import call_command

    output = StringIO()
    call_command("role_matrix", stdout=output)
    generated = [line.rstrip() for line in output.getvalue().splitlines() if line.startswith("|")]
    committed = [
        line.rstrip()
        for line in Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("docs/access-matrix.md")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.startswith("|")
    ]
    assert committed == generated


def test_the_matrix_records_where_a_view_narrows_further(erp):
    """Holding the permission is necessary but not always sufficient, and the matrix says so."""
    from io import StringIO

    from django.core.management import call_command

    output = StringIO()
    call_command("role_matrix", stdout=output)
    printed = output.getvalue()
    assert "Also enforced" in printed
    assert "managers only" in printed
    assert "own file unless you hold the roster" in printed


# ---------------------------------------------------------------------- exam screens


def test_results_can_be_filtered_by_term(erp):
    from academics.models import Term
    from examinations.models import Exam

    first_term = Term.objects.create(
        school=erp.school,
        academic_year=erp.year,
        name="First term",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 30),
    )
    second_term = Term.objects.create(
        school=erp.school,
        academic_year=erp.year,
        name="Second term",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 12, 31),
    )
    erp.exam.term = first_term
    erp.exam.save()
    other = Exam.objects.create(
        school=erp.school, academic_year=erp.year, name="Half Yearly", grade_scale=erp.scale, term=second_term
    )
    client = Client()
    client.force_login(erp.admin)
    body = client.get(f"/exams/results/?term={first_term.pk}").content
    assert erp.exam.name.encode() in body
    assert other.name.encode() not in body


def test_the_mark_grid_shows_a_live_class_average(erp):
    from examinations.services import save_mark

    second = Student.objects.create(
        school=erp.school,
        student_id="AVG-1",
        first_name="Second",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    other = Enrollment.objects.create(
        school=erp.school,
        student=second,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=other, score=Decimal(60))
    client = Client()
    client.force_login(erp.teacher)
    body = client.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}").content.decode()
    assert "Class average 70.00/100" in body
    assert "2 of 2 above the pass mark" in body


def test_the_mark_grid_offers_a_bulk_absent_control(erp):
    client = Client()
    client.force_login(erp.teacher)
    body = client.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}").content
    assert b'data-check-all="absent"' in body
    assert b"data-absent" in body


def test_a_teacher_cannot_bulk_print_draft_report_cards(erp):
    from examinations.services import save_mark

    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    client = Client()
    client.force_login(erp.teacher)
    denied = client.get(f"/exams/report-cards.pdf?exam={erp.exam.pk}&section={erp.section.pk}")
    assert denied.status_code == 403

    # A head may still print the drafts, and everyone may once they are published.
    client.force_login(erp.admin)
    assert client.get(f"/exams/report-cards.pdf?exam={erp.exam.pk}&section={erp.section.pk}").status_code == 200


def test_the_verification_page_names_the_exam_and_still_not_the_child(erp):
    from examinations.models import ResultSnapshot
    from examinations.services import publish_exam, save_mark

    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    snapshot = ResultSnapshot.objects.get()
    body = Client().get(f"/exams/verify/{snapshot.verification_code}/").content
    assert b"Term 1" in body
    assert b"Ayesha" not in body
    assert b"80.00" not in body


def test_a_report_card_prints_a_link_someone_can_type(erp):
    from examinations.models import ResultSnapshot
    from examinations.services import publish_exam, save_mark

    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    snapshot = ResultSnapshot.objects.get()
    client = Client()
    client.force_login(erp.admin)
    pdf = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf").content
    assert pdf.startswith(b"%PDF")
    # ReportLab compresses the page stream, so check the text was handed in whole.
    from examinations.views import verification_url

    fake = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").wsgi_request
    assert verification_url(fake, snapshot).endswith(f"/exams/verify/{snapshot.verification_code}/")
    assert verification_url(fake, snapshot).startswith("http")


# Imported late so the timetable fixture above can use it.
from timetable.models import Period  # noqa: E402
