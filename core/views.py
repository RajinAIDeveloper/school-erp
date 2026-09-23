from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView, UpdateView

from academics.models import AcademicYear

from .access import require_permission
from .forms import FinancePolicyForm, NotificationSettingsForm, SchoolForm, SMSSettingsForm
from .mixins import ERPPermissionMixin
from .models import AuditLog, audit


@login_required
def dashboard(request):
    """Whatever this person signed in to find out."""
    from academics.models import AcademicYear

    from . import dashboards

    school = request.school
    if school is None:
        return render(request, "core/no_school.html")
    if not school.is_active:
        raise PermissionDenied("This school is not active.")

    # A student or guardian is not staff; home shows their own records, not the school's.
    if hasattr(request.user, "student_profile") or hasattr(request.user, "guardian_profile"):
        from core.portal_views import index as portal_index

        return portal_index(request)

    panel = dashboards.build(school, request.user)
    return render(
        request,
        "core/dashboard.html",
        {
            "panel": panel,
            "year": AcademicYear.current_for(school),
            "today": timezone.localdate(),
            "page_title": "Dashboard",
        },
    )


class SchoolSettingsView(ERPPermissionMixin, UpdateView):
    permission_required = "core.change_school"
    form_class = SchoolForm
    template_name = "core/settings_school.html"

    def get_object(self, queryset=None):
        if self.request.school is None:
            from django.core.exceptions import PermissionDenied

            raise PermissionDenied("Attach your account to a school first.")
        return self.request.school

    def form_valid(self, form):
        messages.success(self.request, "School settings saved.")
        return super().form_valid(form)

    def get_success_url(self):
        return self.request.path


class SMSSettingsView(SchoolSettingsView):
    form_class = SMSSettingsForm
    template_name = "core/settings_sms.html"


class NotificationSettingsView(SchoolSettingsView):
    form_class = NotificationSettingsForm
    template_name = "core/settings_notifications.html"

    def form_valid(self, form):
        from messaging.notifications import ensure_default_templates

        response = super().form_valid(form)
        created = ensure_default_templates(self.request.school)
        if created:
            messages.info(
                self.request,
                f"Added {len(created)} message template(s) you can now edit in the school's own words.",
            )
        audit(self.request, "settings.notifications_changed", self.request.school)
        return response


class PolicySettingsView(SchoolSettingsView):
    form_class = FinancePolicyForm
    template_name = "core/settings_policy.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        audit(self.request, "settings.policy_changed", self.request.school)
        return response


class AuditLogListView(ERPPermissionMixin, ListView):
    permission_required = "core.view_auditlog"
    template_name = "core/audit_log.html"
    paginate_by = 50

    def get_queryset(self):
        return AuditLog.objects.filter(school=self.request.school).select_related("user")


@require_permission("academics.change_academicyear")
@require_POST
def switch_year(request, pk):
    """Make one academic year current. Every other year of the school is stood down."""
    year = get_object_or_404(AcademicYear, school=request.school, pk=pk)
    if not year.is_current:
        year.is_current = True
        year.save()
        audit(request, "academic_year.switched", year, f"{year} is now the current session.")
    messages.success(request, f"{year} is now the current academic year.")
    return redirect("settings:year_list")


SETTINGS_CARDS = [
    {
        "title": "School profile",
        "description": "Name, logo, address, currency, weekend days and the head of institution.",
        "url": "settings:school",
        "permission": "core.change_school",
    },
    {
        "title": "Academic years",
        "description": "Sessions and terms, and which year the school is currently running.",
        "url": "settings:year_list",
        "permission": "academics.view_academicyear",
    },
    {
        "title": "Classes and sections",
        "description": "The classes you teach, their sections and who each class teacher is.",
        "url": "settings:class_list",
        "permission": "academics.view_classlevel",
    },
    {
        "title": "Subjects and teaching",
        "description": "Subjects, which classes take them, and which teacher takes each section.",
        "url": "settings:subject_list",
        "permission": "academics.view_subject",
    },
    {
        "title": "People setup",
        "description": "Departments, designations and leave types for staff records.",
        "url": "settings:department_list",
        "permission": "employees.view_department",
    },
    {
        "title": "Fees setup",
        "description": "Fee heads, class structures and concessions.",
        "url": "fees:category_list",
        "permission": "fees.view_feecategory",
    },
    {
        "title": "Chart of accounts",
        "description": "Account heads, opening balances and the ledger's structure.",
        "url": "finance:account_list",
        "permission": "finance.view_account",
    },
    {
        "title": "Grading",
        "description": "Grade scales and the marks each letter and grade point covers.",
        "url": "examinations:scale_list",
        "permission": "examinations.view_gradescale",
    },
    {
        "title": "Periods and rooms",
        "description": "The school day's periods, breaks and teaching rooms.",
        "url": "timetable:period_list",
        "permission": "timetable.change_period",
    },
    {
        "title": "Notifications",
        "description": "Which events text a family, and the wording each message uses.",
        "url": "settings:notifications",
        "permission": "core.change_school",
    },
    {
        "title": "SMS gateway",
        "description": "Provider credentials, sender ID and a live test.",
        "url": "settings:sms",
        "permission": "core.change_school",
    },
    {
        "title": "Policy",
        "description": "Late fees, closing the books and staff self check-in.",
        "url": "settings:policy",
        "permission": "core.change_school",
    },
    {
        "title": "User management",
        "description": "Accounts, roles and passwords.",
        "url": "users:list",
        "permission": "users.view_user",
    },
    {
        "title": "Audit log",
        "description": "Who changed money, marks, attendance and settings, and when.",
        "url": "settings:audit",
        "permission": "core.view_auditlog",
    },
]


@require_permission(None)
def settings_hub(request):
    """One place to find every setup screen the current role may open."""
    from django.urls import reverse

    cards = [
        {**card, "href": reverse(card["url"])} for card in SETTINGS_CARDS if request.user.has_perm(card["permission"])
    ]
    if not cards:
        raise PermissionDenied("You do not have access to any settings.")
    ready = _readiness(request.school)
    return render(
        request,
        "core/settings_hub.html",
        {
            "cards": cards,
            "readiness": ready,
            "can_initialise": request.user.has_perm("core.change_school"),
            "page_title": "Basic Settings",
        },
    )


def _readiness(school):
    """What a school still needs before the day-to-day screens are usable."""
    from academics.models import AcademicYear, ClassLevel, Section, Subject
    from fees.models import FeeCategory
    from finance.models import Account
    from timetable.models import Period

    checks = [
        (
            "Academic year",
            AcademicYear.objects.filter(school=school, is_current=True).exists(),
            "Set the current session so enrollments, fees and results know where they belong.",
        ),
        ("Classes", ClassLevel.objects.filter(school=school).exists(), "Add the classes the school teaches."),
        (
            "Sections",
            Section.objects.filter(school=school).exists(),
            "Add at least one section per class so students can be placed on a roll.",
        ),
        (
            "Subjects",
            Subject.objects.filter(school=school).exists(),
            "Add subjects before scheduling exams or a routine.",
        ),
        ("Fee heads", FeeCategory.objects.filter(school=school).exists(), "Add fee heads so invoices can be raised."),
        (
            "Accounts",
            Account.objects.filter(school=school).exists(),
            "Create the chart of accounts so money has somewhere to post.",
        ),
        (
            "Periods",
            Period.objects.filter(school=school).exists(),
            "Set the school day's periods before building a routine.",
        ),
    ]
    return [{"name": name, "done": done, "hint": hint} for name, done, hint in checks]


@require_permission("core.change_school")
@require_POST
def initialise_defaults(request):
    """Fill the empty dropdowns a new school starts with."""
    from core.management.commands.setup_school import setup_school

    added = setup_school(request.school)
    summary = ", ".join(f"{value} {key.replace('_', ' ')}" for key, value in added.items() if value)
    audit(request, "settings.defaults_created", request.school, summary)
    messages.success(request, f"Defaults in place: {summary}." if summary else "Everything was already set up.")
    return redirect("settings:hub")


class ERPPasswordChangeView(PasswordChangeView):
    """Clears the "temporary password" flag once the person has chosen their own."""

    def form_valid(self, form):
        response = super().form_valid(form)
        if getattr(self.request.user, "must_change_password", False):
            self.request.user.must_change_password = False
            self.request.user.save(update_fields=["must_change_password"])
            audit(self.request, "user.password_changed", self.request.user)
        return response


def healthz(request):
    """
    A liveness probe for whatever runs this in production.

    It checks the two things whose absence makes the site useless — the database and the
    cache — and answers plainly, without needing a session.
    """
    from django.core.cache import cache
    from django.db import connection
    from django.http import JsonResponse

    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 - the probe must report, not raise
        checks["database"] = f"failed: {exc}"
    try:
        cache.set("healthz", "ok", 5)
        checks["cache"] = "ok" if cache.get("healthz") == "ok" else "failed: value not returned"
    except Exception as exc:  # noqa: BLE001
        checks["cache"] = f"failed: {exc}"

    healthy = all(value == "ok" for value in checks.values())
    return JsonResponse({"status": "ok" if healthy else "unhealthy", "checks": checks}, status=200 if healthy else 503)
