"""
Financial statements built from the ledger.

Every figure here is derived from posted journal lines, so a statement can always be
traced back to the entries that produced it. Reversed entries stay in the arithmetic:
a reversal is itself a pair of lines that cancels the original, which is what makes the
history auditable rather than merely tidy.
"""

from decimal import Decimal

from django.db.models import Q, Sum

from .models import ZERO, Account, JournalEntry, JournalLine

COUNTED_STATUSES = [JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED]


def _lines(school, start=None, end=None):
    lines = JournalLine.objects.filter(account__school=school, entry__school=school, entry__status__in=COUNTED_STATUSES)
    if start:
        lines = lines.filter(entry__date__gte=start)
    if end:
        lines = lines.filter(entry__date__lte=end)
    return lines


def account_totals(school, start=None, end=None):
    """{account_id: (debit, credit)} for the period, in one query."""
    rows = _lines(school, start, end).values("account").annotate(debit=Sum("debit"), credit=Sum("credit"))
    return {row["account"]: (row["debit"] or ZERO, row["credit"] or ZERO) for row in rows}


def signed_balance(account, debit, credit):
    """Positive means "more of what this account normally holds"."""
    return debit - credit if account.debit_normal else credit - debit


def trial_balance(school, start=None, end=None):
    """
    Every account with a movement, in debit and credit columns.

    The two columns must agree. If they ever do not, something has written to the ledger
    without going through a balanced entry, and that is worth shouting about.
    """
    totals = account_totals(school, start, end)
    accounts = Account.objects.filter(school=school).order_by("code")
    rows, total_debit, total_credit = [], ZERO, ZERO
    for account in accounts:
        debit, credit = totals.get(account.pk, (ZERO, ZERO))
        if not debit and not credit:
            continue
        net = debit - credit
        row_debit = net if net > ZERO else ZERO
        row_credit = -net if net < ZERO else ZERO
        total_debit += row_debit
        total_credit += row_credit
        rows.append(
            {
                "account": account,
                "debit": row_debit,
                "credit": row_credit,
                "movement_debit": debit,
                "movement_credit": credit,
            }
        )
    return {
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": total_debit == total_credit,
    }


def income_statement(school, start=None, end=None):
    """What the school earned and spent in the period, and what is left."""
    totals = account_totals(school, start, end)
    income, expense = [], []
    for account in Account.objects.filter(
        school=school, account_type__in=[Account.Type.INCOME, Account.Type.EXPENSE]
    ).order_by("code"):
        debit, credit = totals.get(account.pk, (ZERO, ZERO))
        amount = signed_balance(account, debit, credit)
        if amount == ZERO:
            continue
        (income if account.account_type == Account.Type.INCOME else expense).append(
            {"account": account, "amount": amount}
        )
    total_income = sum((row["amount"] for row in income), start=ZERO)
    total_expense = sum((row["amount"] for row in expense), start=ZERO)
    return {
        "income": income,
        "expense": expense,
        "total_income": total_income,
        "total_expense": total_expense,
        "surplus": total_income - total_expense,
    }


def balance_sheet(school, as_of=None):
    """
    Assets against liabilities, equity and the surplus earned to date.

    Income and expense are not shown as lines; their net becomes retained surplus, which
    is what makes the two sides agree.
    """
    totals = account_totals(school, None, as_of)
    groups = {Account.Type.ASSET: [], Account.Type.LIABILITY: [], Account.Type.EQUITY: []}
    surplus = ZERO
    for account in Account.objects.filter(school=school).order_by("code"):
        debit, credit = totals.get(account.pk, (ZERO, ZERO))
        amount = signed_balance(account, debit, credit)
        if account.account_type in groups:
            if amount != ZERO:
                groups[account.account_type].append({"account": account, "amount": amount})
        elif account.account_type == Account.Type.INCOME:
            surplus += amount
        elif account.account_type == Account.Type.EXPENSE:
            surplus -= amount
    total_assets = sum((row["amount"] for row in groups[Account.Type.ASSET]), start=ZERO)
    total_liabilities = sum((row["amount"] for row in groups[Account.Type.LIABILITY]), start=ZERO)
    total_equity = sum((row["amount"] for row in groups[Account.Type.EQUITY]), start=ZERO)
    return {
        "assets": groups[Account.Type.ASSET],
        "liabilities": groups[Account.Type.LIABILITY],
        "equity": groups[Account.Type.EQUITY],
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "total_equity": total_equity,
        "surplus": surplus,
        "total_funding": total_liabilities + total_equity + surplus,
        "balanced": total_assets == total_liabilities + total_equity + surplus,
    }


def ledger(account, start=None, end=None):
    """One account's movements with an opening balance and a running total."""
    school = account.school
    opening = ZERO
    if start:
        rows = (
            _lines(school, None, None)
            .filter(account=account, entry__date__lt=start)
            .aggregate(debit=Sum("debit"), credit=Sum("credit"))
        )
        opening = signed_balance(account, rows["debit"] or ZERO, rows["credit"] or ZERO)
    movements = (
        _lines(school, start, end)
        .filter(account=account)
        .select_related("entry")
        .order_by("entry__date", "entry__entry_no", "id")
    )
    running = opening
    entries = []
    for line in movements:
        running += signed_balance(account, line.debit, line.credit)
        entries.append(
            {
                "date": line.entry.date,
                "entry": line.entry,
                "narration": line.description or line.entry.narration,
                "reference": line.entry.reference,
                "debit": line.debit,
                "credit": line.credit,
                "balance": running,
            }
        )
    return {"account": account, "opening": opening, "rows": entries, "closing": running}


def cash_book(school, start=None, end=None):
    """Money in and out of every cash, bank and mobile account, day by day."""
    cash_accounts = list(Account.objects.filter(school=school, is_cash=True).order_by("code"))
    if not cash_accounts:
        return {"accounts": [], "rows": [], "opening": ZERO, "closing": ZERO, "total_in": ZERO, "total_out": ZERO}
    opening = ZERO
    if start:
        prior = (
            _lines(school)
            .filter(account__in=cash_accounts, entry__date__lt=start)
            .aggregate(debit=Sum("debit"), credit=Sum("credit"))
        )
        opening = (prior["debit"] or ZERO) - (prior["credit"] or ZERO)
    daily = (
        _lines(school, start, end)
        .filter(account__in=cash_accounts)
        .values("entry__date")
        .annotate(received=Sum("debit"), paid=Sum("credit"))
        .order_by("entry__date")
    )
    running = opening
    rows = []
    total_in = total_out = ZERO
    for row in daily:
        received, paid = row["received"] or ZERO, row["paid"] or ZERO
        running += received - paid
        total_in += received
        total_out += paid
        rows.append({"date": row["entry__date"], "received": received, "paid": paid, "balance": running})
    return {
        "accounts": cash_accounts,
        "rows": rows,
        "opening": opening,
        "closing": running,
        "total_in": total_in,
        "total_out": total_out,
    }


def unbalanced_entries(school):
    """Posted entries whose debits and credits disagree. There should never be any."""
    suspect = (
        JournalEntry.objects.filter(school=school, status__in=COUNTED_STATUSES)
        .annotate(debit=Sum("lines__debit"), credit=Sum("lines__credit"))
        .exclude(debit=None)
        .filter(~Q(debit=Decimal("0")) | ~Q(credit=Decimal("0")))
    )
    return [entry for entry in suspect if entry.debit != entry.credit]
