from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from core import views
from core.media import image
from core.security import RateLimitedAuthenticationForm
from examinations import views as exam_views

# Django admin is reserved for platform superusers. School admins use scoped ERP views.
admin.site.has_permission = lambda request: request.user.is_active and request.user.is_superuser
urlpatterns = [
    path("admin/", admin.site.urls),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html", authentication_form=RateLimitedAuthenticationForm
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/change/", views.ERPPasswordChangeView.as_view(), name="password_change"),
    path("password/change/done/", auth_views.PasswordChangeDoneView.as_view(), name="password_change_done"),
    path("password/reset/", auth_views.PasswordResetView.as_view(), name="password_reset"),
    path("password/reset/done/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path(
        "password/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"
    ),
    path("password/reset/complete/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("healthz/", views.healthz, name="healthz"),
    path("", views.dashboard, name="dashboard"),
    path("language/", views.set_language, name="set_language"),
    path("platform/", views.platform, name="platform"),
    path("results/<slug:slug>/", exam_views.public_results, name="public_results"),
    path("academics/", include("academics.urls")),
    path("students/", include("students.urls")),
    path("employees/", include("employees.urls")),
    path("attendance/", include("attendance.urls")),
    path("fees/", include("fees.urls")),
    path("finance/", include("finance.urls")),
    path("exams/", include("examinations.urls")),
    path("routine/", include("timetable.urls")),
    path("downloads/", include("downloads.urls")),
    path("sms/", include("messaging.urls")),
    path("holidays/", include("holidays.urls")),
    path("settings/", include("core.urls")),
    path("analytics/", include("analytics.urls")),
    path("homework/", include("homework.urls")),
    path("users/", include("users.urls")),
    path("reports/", include("reports.urls")),
    path("portal/", include("core.portal_urls")),
    path("media/<path:path>", image, name="protected_image"),
]
