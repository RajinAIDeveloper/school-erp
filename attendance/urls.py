from django.urls import path

from . import views

app_name = "attendance"
urlpatterns = [
    path("", views.student_take, name="student_take"),
    path("report/", views.report, name="student_report"),
    path("staff/", views.staff_take, name="staff_take"),
    path("staff/report/", views.report, {"staff": True}, name="staff_report"),
    path("leave/", views.leaves, name="leave_list"),
    path("leave/new/", views.leave_create, name="leave_create"),
    path("leave/<int:pk>/review/", views.leave_review, name="leave_review"),
]
