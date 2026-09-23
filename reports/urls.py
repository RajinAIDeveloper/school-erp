from django.urls import path

from . import views

app_name = "reports"
urlpatterns = [
    path("", views.hub, name="hub"),
    path("overview/", views.overview, name="overview"),
    path("strength/", views.strength, name="strength"),
    path("payroll/", views.payroll_register, name="payroll_register"),
    path("leave/", views.leave_register, name="leave_register"),
    path("teacher-load/", views.teacher_load, name="teacher_load"),
]
