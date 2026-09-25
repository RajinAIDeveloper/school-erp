from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView, UpdateView

from academics.models import AcademicYear

from .access import platform_admin, require_permission
from .forms import FinancePolicyForm, NotificationSettingsForm, PaymentSettingsForm, SchoolForm, SMSSettingsForm
from .mixins import ERPPermissionMixin
from .models import AuditLog, audit
from .modules import has_module


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


class PaymentSettingsView(SchoolSettingsView):
    form_class = PaymentSettingsForm
    template_name = "core/settings_payments.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        audit(self.request, "settings.payments_changed", self.request.school)
        return response


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
        "description": "Add the class levels your school teaches, such as Class 7.",
        "url": "settings:class_list",
        "permission": "academics.view_classlevel",
    },
    {
        "title": "Sections",
        "description": "Add A, B or other sections under a class and choose each class teacher.",
        "url": "settings:section_list",
        "permission": "academics.view_section",
    },
    {
        "title": "Subjects and teaching",
        "description": "Subjects, which classes take them, and which teacher takes each section.",
        "url": "settings:subject_list",
        "permission": "academics.view_subject",
    },
    {
        "title": "Subject plans",
        "description": "Which subjects each class takes in a year, groups and choices, copied from last year or started from a preset.",
        "url": "academics:subject_plan",
        "permission": "academics.view_classsubject",
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
        "title": "Homework",
        "description": "How many minutes of homework each class should have due on one day.",
        "url": "homework:limits",
        "permission": "homework.change_dailylimit",
        "module": "homework",
    },
    {
        "title": "Admissions",
        "description": "Admission rounds, and how long applications that did not lead to a place are kept.",
        "url": "admissions:settings",
        "permission": "admissions.change_admissionround",
        "module": "admissions",
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
        "title": "Online payments",
        "description": "The school's SSLCommerz account, or the demonstration gateway for a showcase.",
        "url": "settings:payments",
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
        {**card, "href": reverse(card["url"])}
        for card in SETTINGS_CARDS
        if request.user.has_perm(card["permission"])
        and (not card.get("module") or has_module(request.school, card["module"]))
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

    The answer is deliberately two words per check. This endpoint has no sign-in, so a
    database driver's exception text, which readily carries a host name, a port, a user
    and sometimes a query, is not something to hand to whoever asks. The detail goes to
    the server log, where the people who can act on it are already looking.
    """
    import logging

    from django.core.cache import cache
    from django.db import connection
    from django.http import JsonResponse

    log = logging.getLogger(__name__)
    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception:  # noqa: BLE001 - the probe must report, not raise
        log.exception("Health check: the database is not answering")
        checks["database"] = "failed"
    try:
        cache.set("healthz", "ok", 5)
        checks["cache"] = "ok" if cache.get("healthz") == "ok" else "failed"
        if checks["cache"] == "failed":
            log.error("Health check: the cache accepted a write but did not return it")
    except Exception:  # noqa: BLE001
        log.exception("Health check: the cache is not answering")
        checks["cache"] = "failed"

    healthy = all(value == "ok" for value in checks.values())
    return JsonResponse({"status": "ok" if healthy else "unhealthy", "checks": checks}, status=200 if healthy else 503)


@require_POST
@require_permission(None)
def set_language(request):
    """The header toggle: remember this person's language and go back where they were."""
    from core.security import safe_next

    choice = request.POST.get("language", "")
    if choice in ("en", "bn") and (choice == "en" or request.school.bangla_enabled):
        request.user.language = choice
        request.user.save(update_fields=["language"])
    return redirect(safe_next(request, request.POST.get("next"), "/"))


@platform_admin
def platform(request):
    """
    The platform administrator's page: every school, which modules each has been given, and
    which school the administrator is working in.
    """
    from .models import School
    from .modules import MODULES

    if request.method == "POST":
        school = get_object_or_404(School, pk=request.POST.get("school"))
        action = request.POST.get("action", "")
        if action == "work_in":
            request.session["platform_school"] = school.pk
            messages.success(request, f"You are now working in {school}.")
        elif action in ("enable", "disable") and request.POST.get("module") in MODULES:
            key = request.POST["module"]
            module = MODULES[key]
            on = action == "enable"
            if getattr(school, module["field"]) != on:
                setattr(school, module["field"], on)
                school.save(update_fields=[module["field"], "updated_at"])
                AuditLog.objects.create(
                    school=school,
                    user=request.user,
                    action=f"platform.module_{'enabled' if on else 'disabled'}",
                    model=School._meta.label,
                    object_id=str(school.pk),
                    description=f"{module['label']} {'given to' if on else 'taken from'} {school}",
                )
            messages.success(request, f"{module['label']} is {'on' if on else 'off'} for {school}.")
        return redirect("platform")
    schools = [
        {
            "school": school,
            "modules": [(key, module["label"], getattr(school, module["field"])) for key, module in MODULES.items()],
        }
        for school in School.objects.order_by("name")
    ]
    return render(
        request,
        "core/platform.html",
        {"schools": schools, "modules": MODULES, "page_title": "Platform"},
    )
