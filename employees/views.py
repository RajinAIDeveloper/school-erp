"""Teacher and staff records: roster, personal file, documents."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.access import is_manager, require_permission
from core.exports import spreadsheet
from core.generic import ERPCreateView, ERPListView, ERPUpdateView
from core.models import audit

from .forms import EmployeeDocumentForm, EmployeeForm
from .models import Department, Designation, Employee, EmployeeDocument

SALARY_PERMISSION = "finance.view_payroll"


class EmployeeListView(ERPListView):
    model = Employee
    permission_required = "employees.view_employee"
    page_title = "Teachers & staff"
    search_fields = ("first_name", "last_name", "employee_id", "phone")
    select_related = ("department", "designation")
    create_url_name = "employees:create"
    update_url_name = "employees:update"
    detail_url_name = "employees:detail"
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

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["can_see_salary"] = self.request.user.has_perm("finance.change_payroll")
        return kwargs


class EmployeeCreateView(EmployeeFormMixin, ERPCreateView):
    permission_required = "employees.add_employee"
    page_title = "Add employee"

    def get_initial(self):
        return {
            "employee_id": Employee.next_employee_id(self.request.school),
            "joining_date": timezone.localdate(),
        }


class EmployeeUpdateView(EmployeeFormMixin, ERPUpdateView):
    permission_required = "employees.change_employee"
    page_title = "Edit employee"


def _may_open(user, employee):
    """The roster is for managers; a personal file is also for the person it belongs to."""
    return employee.user_id == user.pk or user.has_perm("employees.view_employee")


@require_permission(None)
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
    form = EmployeeDocumentForm(request.POST or None, request.FILES or None, school=request.school)
    form.instance.employee = employee
    if request.method == "POST" and form.is_valid():
        form.instance.uploaded_by = request.user
        form.save()
        audit(request, "employee_document.uploaded", form.instance, f"{form.instance.title} for {employee}")
        messages.success(request, "Document uploaded.")
        return redirect("employees:detail", pk=pk)
    return render(request, "generic/form.html", {"form": form, "page_title": f"Upload document for {employee}"})


@require_permission(None)
def document_download(request, pk):
    """Staff files are visible to managers, and to the employee they belong to."""
    document = get_object_or_404(EmployeeDocument.objects.select_related("employee"), school=request.school, pk=pk)
    own = document.employee.user_id == request.user.pk
    if not (own or is_manager(request.user) or request.user.has_perm("employees.change_employee")):
        raise Http404
    return FileResponse(document.file.open("rb"), as_attachment=True, filename=document.file.name.rsplit("/", 1)[-1])


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
