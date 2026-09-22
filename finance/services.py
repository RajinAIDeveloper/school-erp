"""
Ledger operations.

Posting is the only way money enters the books, and a posted entry is never edited: it is
reversed. The period lock exists so that once a month is reported on, nobody can quietly
change what it said.
"""

from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.access import assert_actor_school, assert_school
from core.models import AuditLog, School

from .models import ZERO, Account, JournalEntry, JournalLine, Payroll, ensure_default_accounts

PAY_METHOD_ACCOUNTS = {"cash": "1010", "bank": "1020", "mobile": "1030"}


def assert_period_open(school, date):
    """Refuse to write into a period the school has closed."""
    locked_until = school.books_locked_until
    if locked_until and date <= locked_until:
        raise ValidationError(
            f"The books are closed up to {locked_until:%d %b %Y}. Post the correction in an open period."
        )


@transaction.atomic
def post_journal(*, school, user, date, narration, lines, source=JournalEntry.Source.MANUAL, reference=""):
    """
    Post a balanced entry of any number of lines.

    `lines` is an iterable of (account, debit, credit). Debits must equal credits and the
    total must be positive, because an entry that changes nothing is not a record of
    anything.
    """
    assert_actor_school(user, school)
    if not user.has_perm("finance.add_journalentry"):
        raise PermissionDenied
    assert_period_open(school, date)

    cleaned = []
    total_debit = total_credit = ZERO
    for account, debit, credit in lines:
        debit, credit = Decimal(debit or 0), Decimal(credit or 0)
        if debit < ZERO or credit < ZERO:
            raise ValidationError("Amounts cannot be negative. Use the other column instead.")
        if debit and credit:
            raise ValidationError(f"{account}: a line is either a debit or a credit, not both.")
        if not debit and not credit:
            continue
        assert_school(school, account)
        if not account.is_active:
            raise ValidationError(f"{account} is no longer in use.")
        cleaned.append((account, debit, credit))
        total_debit += debit
        total_credit += credit

    if len(cleaned) < 2:
        raise ValidationError("An entry needs at least two lines.")
    if total_debit != total_credit:
        raise ValidationError(f"Debits ({total_debit}) and credits ({total_credit}) must be equal.")
    if total_debit <= ZERO:
        raise ValidationError("An entry must move a positive amount.")

    School.objects.select_for_update().get(pk=school.pk)
    entry = JournalEntry.objects.create(
        school=school,
        entry_no=JournalEntry.next_entry_no(school),
        date=date,
        narration=narration,
        reference=reference,
        source=source,
        created_by=user,
    )
    JournalLine.objects.bulk_create(
        [JournalLine(entry=entry, account=account, debit=debit, credit=credit) for account, debit, credit in cleaned]
    )
    entry.post()
    AuditLog.objects.create(
        school=school,
        user=user,
        action="journal.posted",
        model=entry._meta.label,
        object_id=str(entry.pk),
        description=f"{entry} for {total_debit}",
    )
    return entry


@transaction.atomic
def reverse_journal(*, school, user, entry, reason):
    assert_actor_school(user, school)
    assert_school(school, entry)
    if not user.has_perm("finance.change_journalentry"):
        raise PermissionDenied
    if not reason.strip():
        raise ValidationError("Give a reason for the reversal; it goes on the record.")
    if entry.source in (JournalEntry.Source.FEE, JournalEntry.Source.SALARY):
        raise ValidationError(
            "Reverse a fee receipt from the collection desk and a salary from payroll, so the "
            "source document and the ledger stay in step."
        )
    assert_period_open(school, timezone.localdate())
    School.objects.select_for_update().get(pk=school.pk)
    locked = JournalEntry.objects.select_for_update().get(pk=entry.pk)
    reversal = locked.reverse(user=user, narration=f"Reversal of {locked}: {reason.strip()}")
    AuditLog.objects.create(
        school=school,
        user=user,
        action="journal.reversed",
        model=locked._meta.label,
        object_id=str(locked.pk),
        description=reason.strip(),
    )
    return reversal


@transaction.atomic
def record_opening_balances(*, school, user, date, balances, equity_code="3010"):
    """
    Seed the ledger when a school starts using the system mid-year.

    Each account's opening figure is posted against capital in one balanced entry, so the
    books start from a known position rather than from nothing.
    """
    assert_actor_school(user, school)
    if not user.has_perm("finance.add_journalentry"):
        raise PermissionDenied
    ensure_default_accounts(school)
    equity = Account.objects.get(school=school, code=equity_code)
    lines, net = [], ZERO
    for account, amount in balances:
        amount = Decimal(amount or 0)
        if amount == ZERO:
            continue
        assert_school(school, account)
        if account.pk == equity.pk:
            raise ValidationError("Capital is the balancing account; give it no opening figure of its own.")
        if account.debit_normal:
            lines.append((account, amount, ZERO))
            net += amount
        else:
            lines.append((account, ZERO, amount))
            net -= amount
    if not lines:
        raise ValidationError("Enter at least one opening balance.")
    if net > ZERO:
        lines.append((equity, ZERO, net))
    elif net < ZERO:
        lines.append((equity, -net, ZERO))
    return post_journal(
        school=school,
        user=user,
        date=date,
        narration="Opening balances",
        lines=lines,
        source=JournalEntry.Source.OPENING,
        reference="OPENING",
    )


@transaction.atomic
def generate_payroll(*, school, user, month):
    """Prepare salary rows for every active employee, from their contracted basic pay."""
    from employees.models import Employee

    assert_actor_school(user, school)
    if not user.has_perm("finance.add_payroll"):
        raise PermissionDenied
    if month.day != 1:
        raise ValidationError("Use the first day of the salary month.")
    created = 0
    for employee in Employee.objects.filter(school=school, status=Employee.Status.ACTIVE):
        if Payroll.objects.filter(employee=employee, month=month).exists():
            continue
        if employee.basic_salary <= ZERO:
            continue
        Payroll.objects.create(school=school, employee=employee, month=month, basic=employee.basic_salary)
        created += 1
    AuditLog.objects.create(
        school=school, user=user, action="payroll.generated", description=f"{created} row(s) for {month:%B %Y}"
    )
    return created


@transaction.atomic
def pay_payroll(*, school, user, payroll, method="cash", paid_on=None):
    """Pay one salary: Dr Salaries & Wages, Cr the account the money actually left."""
    assert_actor_school(user, school)
    assert_school(school, payroll)
    if not user.has_perm("finance.change_payroll"):
        raise PermissionDenied
    if method not in PAY_METHOD_ACCOUNTS:
        raise ValidationError("Choose how the salary was paid.")
    paid_on = paid_on or timezone.localdate()
    assert_period_open(school, paid_on)
    School.objects.select_for_update().get(pk=school.pk)
    payroll = Payroll.objects.select_for_update().get(pk=payroll.pk, school=school)
    if payroll.journal_entry_id:
        return payroll
    if payroll.net <= ZERO:
        raise ValidationError("Net pay must be positive.")
    ensure_default_accounts(school)
    expense = Account.objects.get(school=school, code="5010")
    source = Account.objects.get(school=school, code=PAY_METHOD_ACCOUNTS[method])
    entry = post_journal(
        school=school,
        user=user,
        date=paid_on,
        narration=f"Salary {payroll.month:%B %Y} - {payroll.employee.full_name}"[:250],
        lines=[(expense, payroll.net, ZERO), (source, ZERO, payroll.net)],
        source=JournalEntry.Source.SALARY,
        reference=f"PAY-{payroll.pk}",
    )
    payroll.journal_entry = entry
    payroll.paid_on = paid_on
    payroll.save(update_fields=["journal_entry", "paid_on", "updated_at"])
    AuditLog.objects.create(
        school=school,
        user=user,
        action="payroll.paid",
        model=payroll._meta.label,
        object_id=str(payroll.pk),
        description=f"{payroll.net} by {method}",
    )
    return payroll


@transaction.atomic
def close_books(*, school, user, through):
    """Lock every period up to and including `through`."""
    assert_actor_school(user, school)
    if not user.has_perm("core.change_school"):
        raise PermissionDenied
    if school.books_locked_until and through < school.books_locked_until:
        raise ValidationError(
            f"The books are already closed to {school.books_locked_until:%d %b %Y}; reopening is not supported here."
        )
    school.books_locked_until = through
    school.save(update_fields=["books_locked_until", "updated_at"])
    AuditLog.objects.create(school=school, user=user, action="books.closed", description=f"Locked through {through}")
    return school
