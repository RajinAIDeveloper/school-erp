"""User accounts: the roster, provisioning logins and password resets."""

import json

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from academics.models import Section
from core.access import require_permission
from core.forms import TailwindFormMixin
from core.generic import ERPCreateView, ERPListView, ERPUpdateView
from core.models import audit
from core.roles import ALL_ROLES
from core.security import safe_next

from .forms import ProfileForm, UserForm
from .models import User
from .services import provision_login, provision_section, reset_password

PROFILE_MODELS = {
    "student": ("students", "Student", "students.view_student"),
    "guardian": ("students", "Guardian", "students.view_guardian"),
    "employee": ("employees", "Employee", "employees.view_employee"),
}


class UserListView(ERPListView):
    model = User
    permission_required = "users.view_user"
    page_title = "User management"
    columns = (
        ("Username", "username"),
        ("Name", "get_full_name"),
        ("Roles", "role_names"),
        ("Linked record", "linked_profile"),
        ("Last signed in", "last_login", "datetime"),
        ("Active", "is_active", "bool"),
    )
    search_fields = ("username", "first_name", "last_name", "email", "phone")
    detail_url_name = "users:detail"
    create_url_name = "users:create"
    update_url_name = "users:update"
    extra_actions = (
        ("Create logins for a class", "users:provision_bulk", "users.add_user"),
        ("Audit log", "settings:audit", "core.view_auditlog"),
    )

    def get_filter_choices(self):
        return (
            ("groups__name", "Role", [(name, name) for name in ALL_ROLES]),
            ("is_active", "Active", [("True", "Active"), ("False", "Deactivated")]),
        )

    def get_queryset(self):
        return super().get_queryset().prefetch_related("groups").distinct()


class UserCreateView(ERPCreateView):
    model = User
    form_class = UserForm
    permission_required = "users.add_user"
    page_title = "Create user"
    success_url_name = "users:list"

    def form_valid(self, form):
        response = super().form_valid(form)
        audit(self.request, "user.created", self.object)
        return response


class UserUpdateView(ERPUpdateView):
    model = User
    form_class = UserForm
    permission_required = "users.change_user"
    page_title = "Edit user"
    success_url_name = "users:list"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs if self.request.user.is_superuser else qs.filter(is_superuser=False)

    def form_valid(self, form):
        if form.instance.pk == self.request.user.pk and not form.cleaned_data["is_active"]:
            form.add_error("is_active", "You cannot deactivate your own account.")
            return self.form_invalid(form)
        response = super().form_valid(form)
        audit(self.request, "user.updated", self.object, "Roles, profile or password updated.")
        return response


@require_permission("users.view_user")
def detail(request, pk):
    account = get_object_or_404(User.objects.prefetch_related("groups"), school=request.school, pk=pk)
    return render(
        request,
        "users/detail.html",
        {
            "account": account,
            "profile": account.linked_profile_object,
            "can_manage": request.user.has_perm("users.change_user")
            and (request.user.is_superuser or not account.is_superuser),
            "page_title": account.get_full_name() or account.username,
        },
    )


def _resolve_profile(request, kind, pk):
    """Fetch a student, guardian or employee the caller is allowed to see."""
    from django.apps import apps

    if kind not in PROFILE_MODELS:
        raise Http404
    app_label, model_name, permission = PROFILE_MODELS[kind]
    if not request.user.has_perm(permission):
        raise PermissionDenied
    model = apps.get_model(app_label, model_name)
    return get_object_or_404(model, school=request.school, pk=pk)


@require_permission("users.add_user")
@require_POST
def provision(request, kind, pk):
    """Create a login for one person, straight from their record."""
    profile = _resolve_profile(request, kind, pk)
    try:
        account, password = provision_login(
            school=request.school,
            user=request.user,
            profile=profile,
            send_sms=request.POST.get("send_sms") == "on",
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect(safe_next(request, request.POST.get("next"), "users:list"))
    rows = [
        {
            "person": str(profile),
            "role": account.role_names,
            "identifier": getattr(profile, "student_id", "")
            or getattr(profile, "employee_id", "")
            or getattr(profile, "phone", ""),
            "username": account.username,
            "password": password,
        }
    ]
    return render(
        request,
        "users/credentials.html",
        {
            "rows": rows,
            "download": json.dumps(rows),
            "back": request.POST.get("next") or "/users/",
            "page_title": "New login",
        },
    )


class BulkProvisionForm(TailwindFormMixin, forms.Form):
    section = forms.ModelChoiceField(queryset=Section.objects.none())
    include_guardians = forms.BooleanField(
        required=False, initial=True, label="Also create a login for each primary guardian"
    )
    send_sms = forms.BooleanField(
        required=False,
        label="Text the credentials",
        help_text="Only works when admission messages are switched on in Notifications.",
    )

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["section"].queryset = Section.objects.filter(school=school).select_related("class_level")


@require_permission("users.add_user")
def provision_bulk(request):
    """Give a whole class its logins at once, and print the list exactly once."""
    form = BulkProvisionForm(request.POST or None, school=request.school)
    rows = None
    if request.method == "POST" and form.is_valid():
        rows = provision_section(
            school=request.school,
            user=request.user,
            section=form.cleaned_data["section"],
            include_guardians=form.cleaned_data["include_guardians"],
            send_sms=form.cleaned_data["send_sms"],
        )
        if not rows:
            messages.info(request, "Everyone in that section already has a login.")
        else:
            # The passwords exist in this one response and nowhere else. They are not put
            # in the session: the default session store is a database table, signed but
            # not encrypted, so anything left there is readable by anyone who can read the
            # database. The CSV is built in the browser from the page already on screen.
            return render(
                request,
                "users/credentials.html",
                {"rows": rows, "download": json.dumps(rows), "back": "/users/", "page_title": "New logins"},
            )
    return render(
        request,
        "users/provision_bulk.html",
        {"form": form, "page_title": "Create logins for a class"},
    )


@require_permission("users.change_user")
@require_POST
def reset(request, pk):
    account = get_object_or_404(User, school=request.school, pk=pk)
    try:
        password = reset_password(school=request.school, user=request.user, account=account)
    except (PermissionDenied, ValidationError) as exc:
        messages.error(request, " ".join(getattr(exc, "messages", [str(exc)])))
        return redirect("users:detail", pk=pk)
    return render(
        request,
        "users/credentials.html",
        {
            "rows": [
                {
                    "person": account.get_full_name() or account.username,
                    "role": account.role_names,
                    "identifier": "",
                    "username": account.username,
                    "password": password,
                }
            ],
            "back": f"/users/{account.pk}/",
            "reset": True,
            "page_title": "Temporary password",
        },
    )


@require_permission(None)
def profile(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile saved.")
        return redirect("users:profile")
    return render(request, "generic/form.html", {"form": form, "page_title": "My profile"})
