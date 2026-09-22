"""Journals, ledgers, financial statements, payroll and the period lock."""

from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from core.models import AuditLog
from fees.services import collect_payment
from finance import reports
from finance.models import Account, JournalEntry, Payroll
from finance.services import (
    close_books,
    generate_payroll,
    pay_payroll,
    post_journal,
    record_opening_balances,
    reverse_journal,
)


def account(erp, code):
    return Account.objects.get(school=erp.school, code=code)


def test_multiline_journal_must_balance(erp):
    cash, rent, utilities = account(erp, "1010"), account(erp, "5020"), account(erp, "5030")
    with pytest.raises(ValidationError):
        post_journal(
            school=erp.school,
            user=erp.accountant,
            date=date(2026, 9, 1),
            narration="Unbalanced",
            lines=[(rent, 500, 0), (cash, 0, 400)],
        )
    entry = post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 1),
        narration="Monthly overheads",
        lines=[(rent, 500, 0), (utilities, 300, 0), (cash, 0, 800)],
    )
    assert entry.lines.count() == 3
    assert entry.is_balanced and entry.status == "posted"
    assert AuditLog.objects.filter(action="journal.posted").exists()


def test_journal_rejects_a_two_sided_line_and_a_single_line_entry(erp):
    cash, rent = account(erp, "1010"), account(erp, "5020")
    with pytest.raises(ValidationError):
        post_journal(
            school=erp.school,
            user=erp.accountant,
            date=date(2026, 9, 1),
            narration="Both sides",
            lines=[(rent, 100, 100), (cash, 0, 100)],
        )
    with pytest.raises(ValidationError):
        post_journal(
            school=erp.school,
            user=erp.accountant,
            date=date(2026, 9, 1),
            narration="Lonely",
            lines=[(rent, 100, 0)],
        )
    assert not JournalEntry.objects.exists()


def test_journal_refuses_an_account_from_another_school(erp):
    foreign = Account.objects.create(school=erp.other, code="9999", name="Theirs", account_type="expense")
    with pytest.raises(PermissionDenied):
        post_journal(
            school=erp.school,
            user=erp.accountant,
            date=date(2026, 9, 1),
            narration="Wrong school",
            lines=[(foreign, 100, 0), (account(erp, "1010"), 0, 100)],
        )


def test_journal_screen_posts_from_the_formset(erp):
    client = Client()
    client.force_login(erp.accountant)
    response = client.post(
        "/finance/journals/new/",
        {
            "date": "2026-09-05",
            "narration": "Stationery and printing",
            "reference": "V-11",
            "lines-TOTAL_FORMS": "4",
            "lines-INITIAL_FORMS": "0",
            "lines-MIN_NUM_FORMS": "0",
            "lines-MAX_NUM_FORMS": "30",
            "lines-0-account": account(erp, "5040").pk,
            "lines-0-debit": "700",
            "lines-0-credit": "",
            "lines-0-description": "Exam papers",
            "lines-1-account": account(erp, "1010").pk,
            "lines-1-debit": "",
            "lines-1-credit": "700",
            "lines-1-description": "",
            "lines-2-account": "",
            "lines-2-debit": "",
            "lines-2-credit": "",
            "lines-2-description": "",
            "lines-3-account": "",
            "lines-3-debit": "",
            "lines-3-credit": "",
            "lines-3-description": "",
        },
    )
    assert response.status_code == 302
    assert JournalEntry.objects.get().total_debit == Decimal("700")


def test_quick_entry_records_an_expense(erp):
    client = Client()
    client.force_login(erp.accountant)
    response = client.post(
        "/finance/journals/quick/",
        {
            "kind": "expense",
            "date": "2026-09-06",
            "narration": "Electricity bill",
            "category": account(erp, "5030").pk,
            "paid_from": account(erp, "1010").pk,
            "amount": "1500",
            "reference": "BILL-9",
        },
    )
    assert response.status_code == 302
    assert account(erp, "5030").balance() == Decimal("1500")
    assert account(erp, "1010").balance() == Decimal("-1500")


def test_quick_entry_rejects_the_wrong_kind_of_account(erp):
    client = Client()
    client.force_login(erp.accountant)
    response = client.post(
        "/finance/journals/quick/",
        {
            "kind": "expense",
            "date": "2026-09-06",
            "narration": "Mislabelled",
            "category": account(erp, "4010").pk,
            "paid_from": account(erp, "1010").pk,
            "amount": "100",
        },
    )
    assert response.status_code == 200
    assert not JournalEntry.objects.exists()


def test_reversal_needs_a_reason_and_leaves_both_entries_on_record(erp):
    cash, rent = account(erp, "1010"), account(erp, "5020")
    entry = post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 1),
        narration="Rent",
        lines=[(rent, 500, 0), (cash, 0, 500)],
    )
    with pytest.raises(ValidationError):
        reverse_journal(school=erp.school, user=erp.accountant, entry=entry, reason="  ")
    reverse_journal(school=erp.school, user=erp.accountant, entry=entry, reason="Paid twice")
    entry.refresh_from_db()
    assert entry.status == "reversed"
    assert JournalEntry.objects.count() == 2
    assert rent.balance() == Decimal("0") and cash.balance() == Decimal("0")


def test_fee_and_salary_entries_are_not_reversed_from_the_ledger(erp, invoice):
    payment = collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=500,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    with pytest.raises(ValidationError):
        reverse_journal(school=erp.school, user=erp.accountant, entry=payment.journal_entry, reason="Wrong")
    assert not payment.journal_entry.reversible


def test_trial_balance_agrees_after_fees_expenses_and_a_reversal(erp, invoice):
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=1000,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    entry = post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 22),
        narration="Rent",
        lines=[(account(erp, "5020"), 500, 0), (account(erp, "1010"), 0, 500)],
    )
    reverse_journal(school=erp.school, user=erp.accountant, entry=entry, reason="Landlord refunded")
    data = reports.trial_balance(erp.school)
    assert data["balanced"], (data["total_debit"], data["total_credit"])
    assert not reports.unbalanced_entries(erp.school)


def test_income_statement_and_balance_sheet_agree_with_the_ledger(erp, invoice):
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=1000,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 22),
        narration="Rent",
        lines=[(account(erp, "5020"), 400, 0), (account(erp, "1010"), 0, 400)],
    )
    income = reports.income_statement(erp.school)
    assert income["total_income"] == Decimal("1000")
    assert income["total_expense"] == Decimal("400")
    assert income["surplus"] == Decimal("600")

    sheet = reports.balance_sheet(erp.school)
    assert sheet["balanced"]
    assert sheet["total_assets"] == Decimal("600")
    assert sheet["surplus"] == Decimal("600")


def test_ledger_shows_opening_balance_and_a_running_total(erp):
    cash, rent = account(erp, "1010"), account(erp, "5020")
    post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 8, 1),
        narration="August rent",
        lines=[(rent, 300, 0), (cash, 0, 300)],
    )
    post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 3),
        narration="September rent",
        lines=[(rent, 400, 0), (cash, 0, 400)],
    )
    data = reports.ledger(rent, date(2026, 9, 1), date(2026, 9, 30))
    assert data["opening"] == Decimal("300")
    assert len(data["rows"]) == 1
    assert data["closing"] == Decimal("700")


def test_cash_book_groups_by_day_across_cash_accounts(erp, invoice):
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=600,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 21),
        narration="Bank deposit",
        lines=[(account(erp, "1020"), 200, 0), (account(erp, "1010"), 0, 200)],
    )
    data = reports.cash_book(erp.school, date(2026, 9, 1), date(2026, 9, 30))
    assert len(data["rows"]) == 1
    assert data["closing"] == Decimal("600")


def test_opening_balances_post_against_capital(erp):
    entry = record_opening_balances(
        school=erp.school,
        user=erp.admin,
        date=date(2026, 1, 1),
        balances=[(account(erp, "1010"), Decimal("5000")), (account(erp, "2010"), Decimal("1200"))],
    )
    assert entry.is_balanced
    assert account(erp, "1010").balance() == Decimal("5000")
    assert account(erp, "2010").balance() == Decimal("1200")
    assert account(erp, "3010").balance() == Decimal("3800")
    assert reports.balance_sheet(erp.school)["balanced"]


def test_opening_balances_refuse_capital_itself(erp):
    with pytest.raises(ValidationError):
        record_opening_balances(
            school=erp.school,
            user=erp.admin,
            date=date(2026, 1, 1),
            balances=[(account(erp, "3010"), Decimal("100"))],
        )


def test_payroll_run_prepares_rows_from_contracted_pay_and_skips_existing(erp):
    erp.employee.basic_salary = Decimal("25000")
    erp.employee.save()
    assert generate_payroll(school=erp.school, user=erp.accountant, month=date(2026, 9, 1)) == 1
    assert generate_payroll(school=erp.school, user=erp.accountant, month=date(2026, 9, 1)) == 0
    row = Payroll.objects.get()
    assert row.basic == Decimal("25000")
    with pytest.raises(ValidationError):
        generate_payroll(school=erp.school, user=erp.accountant, month=date(2026, 9, 15))


def test_paying_a_salary_by_bank_debits_the_bank_and_is_idempotent(erp):
    row = Payroll.objects.create(
        school=erp.school,
        employee=erp.employee,
        month=date(2026, 9, 1),
        basic=Decimal("1000"),
        allowance=Decimal("100"),
        deduction=Decimal("50"),
    )
    pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="bank", paid_on=date(2026, 9, 28))
    pay_payroll(school=erp.school, user=erp.accountant, payroll=row, method="bank", paid_on=date(2026, 9, 28))
    assert JournalEntry.objects.filter(source="salary").count() == 1
    assert account(erp, "5010").balance() == Decimal("1050")
    assert account(erp, "1020").balance() == Decimal("-1050")


def test_payslip_pdf_renders(erp):
    row = Payroll.objects.create(
        school=erp.school, employee=erp.employee, month=date(2026, 9, 1), basic=Decimal("1000")
    )
    client = Client()
    client.force_login(erp.accountant)
    assert client.get(f"/finance/payroll/{row.pk}.pdf").content.startswith(b"%PDF")


def test_closing_the_books_blocks_backdated_posting(erp):
    close_books(school=erp.school, user=erp.admin, through=date(2026, 8, 31))
    erp.school.refresh_from_db()
    with pytest.raises(ValidationError):
        post_journal(
            school=erp.school,
            user=erp.accountant,
            date=date(2026, 8, 15),
            narration="Too late",
            lines=[(account(erp, "5020"), 100, 0), (account(erp, "1010"), 0, 100)],
        )
    # An open period still accepts work.
    post_journal(
        school=erp.school,
        user=erp.accountant,
        date=date(2026, 9, 15),
        narration="September rent",
        lines=[(account(erp, "5020"), 100, 0), (account(erp, "1010"), 0, 100)],
    )
    assert JournalEntry.objects.count() == 1


def test_closing_the_books_blocks_a_backdated_fee_receipt(erp, invoice):
    close_books(school=erp.school, user=erp.admin, through=date(2026, 9, 30))
    erp.school.refresh_from_db()
    with pytest.raises(ValidationError):
        collect_payment(
            school=erp.school,
            user=erp.accountant,
            invoice=invoice,
            amount=100,
            method="cash",
            reference="",
            date=date(2026, 9, 21),
        )


def test_only_an_administrator_closes_the_books(erp):
    with pytest.raises(PermissionDenied):
        close_books(school=erp.school, user=erp.accountant, through=date(2026, 8, 31))


@pytest.mark.parametrize(
    "url",
    [
        "/finance/reports/trial-balance/",
        "/finance/reports/income/",
        "/finance/reports/balance-sheet/",
        "/finance/reports/cash-book/",
        "/finance/reports/integrity/",
        "/finance/opening-balances/",
        "/finance/journals/quick/",
        "/finance/payroll/run/",
    ],
)
def test_finance_screens_open_for_the_accountant(erp, url):
    client = Client()
    client.force_login(erp.accountant)
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "pdf"])
def test_statements_export_in_every_format(erp, invoice, fmt):
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=500,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    client = Client()
    client.force_login(erp.accountant)
    for url in ("/finance/reports/trial-balance/", "/finance/reports/income/", "/finance/reports/cash-book/"):
        response = client.get(f"{url}?format={fmt}")
        assert response.status_code == 200 and response.content


def test_teacher_cannot_reach_the_ledger(erp):
    client = Client()
    client.force_login(erp.teacher)
    for url in ("/finance/", "/finance/reports/trial-balance/", "/finance/payroll/"):
        assert client.get(url).status_code == 403
