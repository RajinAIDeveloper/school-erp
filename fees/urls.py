from django.urls import path

from core.crud import crud

from . import views
from .models import FeeCategory, FeeConcession, FeeStructure

app_name = "fees"
urlpatterns = [
    path("", views.InvoiceListView.as_view(), name="invoice_list"),
    path("generate/", views.generate, name="generate"),
    path("<int:pk>/", views.detail, name="invoice_detail"),
    path("receipts/<int:pk>.pdf", views.receipt, name="receipt"),
    path("payments/<int:pk>/cancel/", views.cancel, name="cancel"),
    path("reports/", views.report, name="report"),
    path("statement/<int:student_pk>/", views.statement, name="statement"),
]
for model, key, fields, columns in [
    (
        FeeCategory,
        "category",
        ["name", "income_account", "is_active"],
        [("Category", "name"), ("Income account", "income_account"), ("Active", "is_active", "bool")],
    ),
    (
        FeeStructure,
        "structure",
        ["academic_year", "class_level", "category", "amount", "frequency"],
        [
            ("Year", "academic_year"),
            ("Class", "class_level"),
            ("Category", "category"),
            ("Amount", "amount", "money"),
            ("Frequency", "frequency"),
        ],
    ),
    (
        FeeConcession,
        "concession",
        ["student", "category", "percent", "fixed_amount", "reason", "is_active"],
        [
            ("Student", "student"),
            ("Category", "category"),
            ("Percent", "percent"),
            ("Fixed discount", "fixed_amount", "money"),
            ("Active", "is_active", "bool"),
        ],
    ),
]:
    urlpatterns += crud(
        model, "fees", key, fields, columns, actions=(("Invoices", "fees:invoice_list", "fees.view_feeinvoice"),)
    )
