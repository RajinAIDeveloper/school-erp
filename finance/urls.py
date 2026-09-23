from django.urls import path

from core.crud import crud

from . import views
from .models import Account

app_name = "finance"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("journals/new/", views.journal_create, name="journal_create"),
    path("journals/quick/", views.quick_entry, name="quick_entry"),
    path("journals/<int:pk>/reverse/", views.reverse_entry, name="reverse"),
    path("opening-balances/", views.opening_balances, name="opening_balances"),
    path("accounts/<int:pk>/ledger/", views.account_ledger, name="ledger"),
    path("reports/", views.report, name="report"),
    path("reports/trial-balance/", views.trial_balance, name="trial_balance"),
    path("reports/income/", views.income_statement, name="income_statement"),
    path("reports/balance-sheet/", views.balance_sheet, name="balance_sheet"),
    path("reports/cash-book/", views.cash_book, name="cash_book"),
    path("reports/integrity/", views.integrity, name="integrity"),
    path("close/", views.close_period, name="close_period"),
    path("payroll/", views.payroll, name="payroll"),
    path("payroll/new/", views.payroll_create, name="payroll_create"),
    path("payroll/run/", views.payroll_run, name="payroll_run"),
    path("payroll/<int:pk>/pay/", views.payroll_pay, name="payroll_pay"),
    path("payroll/<int:pk>/reverse/", views.payroll_reverse, name="payroll_reverse"),
    path("payroll/<int:pk>.pdf", views.payslip, name="payslip"),
]
urlpatterns += crud(
    Account,
    "finance",
    "account",
    ["code", "name", "account_type", "parent", "is_cash", "is_active", "description"],
    [
        ("Code", "code"),
        ("Name", "name"),
        ("Type", "get_account_type_display"),
        ("Cash", "is_cash", "bool"),
        ("Active", "is_active", "bool"),
    ],
    search=("code", "name"),
    filters=(("account_type", "Type", Account.Type.choices),),
    actions=(
        ("Accounts", "finance:dashboard", "finance.view_journalentry"),
        ("Trial balance", "finance:trial_balance", "finance.view_account"),
        ("Opening balances", "finance:opening_balances", "finance.add_journalentry"),
    ),
)
