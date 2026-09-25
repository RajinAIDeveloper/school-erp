"""Teacher and staff records: roster, personal file, documents."""

import json

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.access import is_manager, require_permission
from core.exports import spreadsheet
from core.files import too_large
from core.generic import ERPCreateView, ERPListView, ERPUpdateView
from core.models import audit
from users.services import provision_login

from .forms import DOCUMENT_UPLOADS, EmployeeDocumentForm, EmployeeForm
from .models import Department, Designation, Employee, EmployeeDocument

SALARY_PERMISSION = "finance.view_payroll"


def _save_employee_documents(request, employee, form):
    """Attach the optional files submitted with the employee form."""
    for field, category, label in DOCUMENT_UPLOADS:
        content = form.cleaned_data.get(field)
        if content:
            original_name = request.FILES[field].name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            EmployeeDocument.objects.create(
                school=request.school,
                employee=employee,
                category=category,
                title=f"{label}: {original_name}"[:150],
                file=content,
                uploaded_by=request.user,
            )


class EmployeeListView(ERPListView):
    model = Employee
    permission_required = "employees.view_employee"
    page_title = "Teachers & staff"
    search_fields = ("first_name", "last_name", "employee_id", "phone")
    select_related = ("department", "designation")
    create_url_name = "employees:create"
    update_url_name = "employees:update"
    detail_url_name = "employees:detail"
    empty_message = (
        "No staff records found. A Teacher login alone is not a staff record. "
        "Click + New to add a teacher or staff member and create or link their login."
    )
    extra_actions = (
        ("Teaching assignments", "settings:subject_teacher_list", "academics.view_subjectteacher"),
        ("Departments", "settings:department_list", "employees.view_department"),
        ("Designations", "settings:designation_list", "employees.view_designation"),
        ("Export", "employees:export", "employees.view_employee"),
    )

    @property
    def columns(self):
        base = [
            ("ID", "employee_id"),
            ("Name", "full_name"),
            ("Type", "get_employee_type_display"),
            ("Designation", "designation"),
            ("Department", "department"),
            ("Phone", "phone"),
            ("Status", "status", "badge"),
        ]
        if self.request.user.has_perm(SALARY_PERMISSION):
            base.insert(-1, ("Basic salary", "basic_salary", "money"))
        return tuple(base)

    def get_filter_choices(self):
        school = self.request.school
        return (
            ("employee_type", "Type", Employee.Type.choices),
            ("status", "Status", Employee.Status.choices),
            ("department", "Department", [(str(d.pk), d.name) for d in Department.objects.filter(school=school)]),
            ("designation", "Designation", [(str(d.pk), d.name) for d in Designation.objects.filter(school=school)]),
        )


class EmployeeFormMixin:
    model = Employee
    form_class = EmployeeForm
    success_url_name = "employees:list"
    template_name = "employees/form.html"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["can_see_salary"] = self.request.user.has_perm("finance.change_payroll")
        kwargs["can_create_login"] = self.request.user.has_perm("users.add_user")
        kwargs["can_upload_documents"] = self.request.user.has_perm("employees.add_employeedocument")
        return kwargs

    def post(self, request, *args, **kwargs):
        if too_large(request):
            messages.error(request, "The upload is too large. Upload files of up to 10 MB each.")
            return redirect(request.path)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["login_autofill"] = {
            str(user.pk): {
                "first_name": user.first_name,
                "last_name": user.last_name,
                "email": user.email,
                "phone": user.phone,
                "roles": [group.name for group in user.groups.all()],
            }
            for user in context["form"].fields["user"].queryset
        }
        return context


class EmployeeCreateView(EmployeeFormMixin, ERPCreateView):
    permission_required = "employees.add_employee"
    page_title = "Add teacher or staff member"

    def get_initial(self):
        return {
            "employee_id": Employee.next_employee_id(self.request.school),
            "joining_date": timezone.localdate(),
        }

    def form_valid(self, form):
        if not form.cleaned_data.get("create_login") or form.cleaned_data.get("user"):
            with transaction.atomic():
                response = super().form_valid(form)
                if response.status_code == 302:
                    _save_employee_documents(self.request, self.object, form)
                return response
        try:
            with transaction.atomic():
                response = super().form_valid(form)
                if response.status_code != 302:
                    return response
                account, password = provision_login(
                    school=self.request.school, user=self.request.user, profile=self.object
                )
                _save_employee_documents(self.request, self.object, form)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        rows = [
            {
                "person": str(self.object),
                "role": account.role_names,
                "identifier": self.object.employee_id,
                "username": account.username,
                "password": password,
            }
        ]
        return render(
            self.request,
            "users/credentials.html",
            {
                "rows": rows,
                "download": json.dumps(rows),
                "back": reverse("employees:detail", args=[self.object.pk]),
                "page_title": "New staff login",
            },
        )


class EmployeeUpdateView(EmployeeFormMixin, ERPUpdateView):
    permission_required = "employees.change_employee"
    page_title = "Edit employee"

    def form_valid(self, form):
        with transaction.atomic():
            response = super().form_valid(form)
            if response.status_code == 302:
                _save_employee_documents(self.request, self.object, form)
            return response


def _may_open(user, employee):
    """The roster is for managers; a personal file is also for the person it belongs to."""
    return employee.user_id == user.pk or user.has_perm("employees.view_employee")


@require_permission(None, also="own file unless you hold the roster")
def detail(request, pk):
    """One person's file: assignments, attendance, leave and (for payroll staff) salary."""
    from attendance.models import LeaveRequest, StaffAttendance
    from attendance.services import leave_balance

    employee = get_object_or_404(
        Employee.objects.select_related("department", "designation", "user"), school=request.school, pk=pk
    )
    if not _may_open(request.user, employee):
        raise PermissionDenied("You may only open your own staff file.")
    today = timezone.localdate()
    month_start = today.replace(day=1)
    attendance = StaffAttendance.objects.filter(employee=employee, date__gte=month_start)
    summary = attendance.aggregate(total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"])))
    may_see_salary = request.user.has_perm(SALARY_PERMISSION)
    payrolls = employee.payrolls.order_by("-month")[:12] if may_see_salary else []
    return render(
        request,
        "employees/detail.html",
        {
            "employee": employee,
            "page_title": employee.full_name,
            "assignments": employee.subject_assignments.select_related(
                "subject", "section__class_level", "academic_year"
            ),
            "class_teacher_of": employee.class_teacher_of.select_related("class_level"),
            "attendance_total": summary["total"],
            "attendance_present": summary["present"],
            "attendance_pct": round(summary["present"] * 100 / summary["total"]) if summary["total"] else None,
            "recent_attendance": attendance.order_by("-date")[:14],
            "leaves": LeaveRequest.objects.filter(employee=employee).select_related("leave_type")[:10],
            "leave_balance": leave_balance(employee, today.year),
            "documents": employee.documents.all(),
            "payrolls": payrolls,
            "may_see_salary": may_see_salary,
        },
    )


@require_permission(None)
def me(request):
    """A teacher or staff member opening their own file."""
    employee = getattr(request.user, "employee_profile", None)
    if employee is None:
        raise Http404("Your account is not linked to an employee record. Ask the school office to link it.")
    return redirect("employees:detail", pk=employee.pk)


@require_permission("employees.add_employeedocument")
def document_upload(request, pk):
    employee = get_object_or_404(Employee, school=request.school, pk=pk)
    if too_large(request):
        messages.error(request, "The upload is too large. Choose a file of up to 10 MB.")
        return redirect("employees:detail", pk=pk)
    form = EmployeeDocumentForm(request.POST or None, request.FILES or None, school=request.school)
    form.instance.employee = employee
    if request.method == "POST" and form.is_valid():
        form.instance.uploaded_by = request.user
        form.save()
        audit(request, "employee_document.uploaded", form.instance, f"{form.instance.title} for {employee}")
        messages.success(request, "Document uploaded.")
        return redirect("employees:detail", pk=pk)
    return render(
        request,
        "generic/form.html",
        {
            "form": form,
            "page_title": f"Upload document for {employee}",
            "cancel_url": reverse("employees:detail", args=[employee.pk]),
        },
    )


@require_permission(None, also="own documents unless a manager")
def document_download(request, pk):
    """Staff files are visible to managers, and to the employee they belong to."""
    document = get_object_or_404(EmployeeDocument.objects.select_related("employee"), school=request.school, pk=pk)
    own = document.employee.user_id == request.user.pk
    if not (own or is_manager(request.user) or request.user.has_perm("employees.change_employee")):
        raise Http404
    response = FileResponse(document.file.open("rb"), as_attachment=True, filename=document.file.name.rsplit("/", 1)[-1])
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@require_permission("employees.view_employee")
def export(request):
    employees = (
        Employee.objects.filter(school=request.school)
        .select_related("department", "designation")
        .order_by("employee_id")
    )
    headers = [
        "ID",
        "Name",
        "Type",
        "Designation",
        "Department",
        "Phone",
        "Email",
        "NID",
        "Qualification",
        "Joining date",
        "Status",
    ]
    rows = [
        [
            e.employee_id,
            e.full_name,
            e.get_employee_type_display(),
            str(e.designation or ""),
            str(e.department or ""),
            e.phone,
            e.email,
            e.nid,
            e.qualification,
            e.joining_date,
            e.get_status_display(),
        ]
        for e in employees
    ]
    if request.user.has_perm(SALARY_PERMISSION):
        headers.append("Basic salary")
        for row, employee in zip(rows, employees, strict=True):
            row.append(employee.basic_salary)
    return spreadsheet("employees", headers, rows, request.GET.get("format", "csv"))
