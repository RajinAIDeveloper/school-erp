from django.urls import path

from . import views

app_name = "admissions"

urlpatterns = [
    path("", views.home, name="home"),
    path("settings/", views.settings_page, name="settings"),
    path("rounds/new/", views.round_new, name="round_new"),
    path("rounds/<int:pk>/", views.round_detail, name="round"),
    path("rounds/<int:pk>/edit/", views.round_edit, name="round_edit"),
    path("rounds/<int:pk>/classes/new/", views.class_new, name="class_new"),
    path("classes/<int:pk>/", views.pipeline, name="pipeline"),
    path("classes/<int:pk>/edit/", views.class_edit, name="class_edit"),
    path("classes/<int:pk>/apply/", views.office_entry, name="office_entry"),
    path("applications/<int:pk>/", views.application_detail, name="application"),
    path("applications/<int:pk>/edit/", views.application_edit, name="application_edit"),
    path("applications/<int:pk>/slip/", views.office_slip, name="slip"),
    path("documents/<int:pk>/", views.document_file, name="document"),
    path("payments/<int:pk>/receipt/", views.receipt, name="receipt"),
]
