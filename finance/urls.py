from django.urls import path
from core.crud import crud
from . import views
from .models import Account
app_name="finance"
urlpatterns=[
    path("",views.dashboard,name="dashboard"),
    path("journals/new/",views.journal_create,name="journal_create"),
    path("journals/<int:pk>/reverse/",views.reverse_entry,name="reverse"),
    path("reports/",views.report,name="report"),
    path("payroll/",views.payroll,name="payroll"),
    path("payroll/new/",views.payroll_create,name="payroll_create"),
    path("payroll/<int:pk>/pay/",views.payroll_pay,name="payroll_pay"),
    path("payroll/<int:pk>.pdf",views.payslip,name="payslip"),
]
urlpatterns+=crud(Account,"finance","account",["code","name","account_type","parent","is_cash","is_active","description"],
    [("Code","code"),("Name","name"),("Type","account_type"),("Balance","balance","money")])
