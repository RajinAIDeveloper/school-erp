"""Student roster, admission, records and leaving screens."""

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from academics.models import ClassLevel, Section
from core.access import require_permission, students_for
from core.exports import spreadsheet
from core.generic import ERPCreateView, ERPListView, ERPUpdateView
from core.models import audit

from .forms import (
    AdmissionForm,
    DocumentForm,
    GuardianForm,
    ImportForm,
    PromotionForm,
    StatusChangeForm,
    StudentForm,
)
from .models import Enrollment, Student, StudentDocument, StudentGuardian
from .services import admit, change_status, import_students, promote, read_import_rows, validate_import

IMPORT_SESSION_KEY = "student_import_rows"


class StudentListView(ERPListView):
    model = Student
    permission_required = "students.view_student"
    page_title = "Students"
    search_fields = ("first_name", "last_name", "student_id", "phone", "guardian_links__guardian__phone")
    columns = (
        ("ID", "student_id"),
        ("Name", "full_name"),
        ("Class", "current_enrollment.section"),
        ("Roll", "current_enrollment.roll_number"),
        ("Gender", "get_gender_display"),
        ("Guardian", "primary_guardian.phone"),
        ("Status", "status", "badge"),
    )
    create_url_name = "students:create"
    update_url_name = "students:update"
    detail_url_name = "students:detail"
    extra_actions = (
        ("Admit student", "students:admission", "students.add_student"),
        ("Enrollments", "students:enrollment_list", "students.view_enrollment"),
        ("Promote", "students:promote", "students.change_enrollment"),
        ("Import CSV", "students:import", "students.add_student"),
        ("Export", "students:export", "students.view_student"),
    )

    def get_filter_choices(self):
        school = self.request.school
        return (
            (
                "enrollments__class_level",
                "Class",
                [(str(c.pk), c.name) for c in ClassLevel.objects.filter(school=school)],
            ),
            (
                "enrollments__section",
                "Section",
                [(str(s.pk), str(s)) for s in Section.objects.filter(school=school).select_related("class_level")],
            ),
            ("status", "Status", Student.Status.choices),
            ("gender", "Gender", Student.gender.field.choices),
        )

    def scope_queryset(self, qs):
        return qs.filter(pk__in=students_for(self.request.user, self.request.school))

    def get_queryset(self):
        current = Prefetch(
            "enrollments",
            queryset=Enrollment.objects.filter(academic_year__is_current=True).select_related(
                "section__class_level", "academic_year"
            ),
            to_attr="current_enrollments",
        )
        guardians = Prefetch(
            "guardian_links",
            queryset=StudentGuardian.objects.select_related("guardian").order_by("-is_primary"),
            to_attr="ordered_guardian_links",
        )
        return super().get_queryset().prefetch_related(current, guardians).distinct()


class StudentCreateView(ERPCreateView):
    model = Student
    form_class = StudentForm
    permission_required = "students.add_student"
    success_url_name = "students:list"
    page_title = "Add student record"

    def get_initial(self):
        from academics.models import AcademicYear

        year = AcademicYear.current_for(self.request.school)
        return {
            "student_id": Student.next_student_id(
                self.request.school, year.name if year else str(timezone.localdate().year)
            ),
            "admission_date": timezone.localdate(),
        }


class StudentUpdateView(ERPUpdateView):
    model = Student
    form_class = StudentForm
    permission_required = "students.change_student"
    success_url_name = "students:list"
    page_title = "Edit student"

    def scope_queryset(self, qs):
        return qs.filter(pk__in=students_for(self.request.user, self.request.school))


@require_permission("students.add_student")
def admission(request):
    """Student, guardian and class placement captured on one screen."""
    form = AdmissionForm(request.POST or None, request.FILES or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            student, _guardian, enrollment = admit(
                school=request.school,
                user=request.user,
                student=form.save(commit=False),
                guardian_data={
                    "full_name": data["guardian_name"],
                    "phone": data["guardian_phone"],
                    "relation": data["guardian_relation"],
                    "nid": data["guardian_nid"],
                    "email": data["guardian_email"],
                    "occupation": data["guardian_occupation"],
                },
                enrollment_data={
                    "academic_year": data["academic_year"],
                    "section": data["section"],
                    "roll_number": data["roll_number"],
                },
            )
            messages.success(
                request, f"Admitted {student.full_name} to {enrollment.section} as roll {enrollment.roll_number}."
            )
            return redirect("students:detail", pk=student.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "students/admission.html", {"form": form, "page_title": "Admit a student"})


@require_permission(None, also="own children or your own sections")
def detail(request, pk):
    from attendance.models import StudentAttendance
    from examinations.models import Exam
    from fees.models import FeeInvoice

    student = get_object_or_404(
        students_for(request.user, request.school).prefetch_related("guardian_links__guardian"), pk=pk
    )
    enrollments = student.enrollments.select_related("section__class_level", "academic_year")
    current = student.current_enrollment
    attendance = StudentAttendance.objects.filter(enrollment__student=student)
    if current:
        attendance = attendance.filter(enrollment__academic_year=current.academic_year)
    summary = attendance.aggregate(total=Count("id"), present=Count("id", filter=Q(status__in=["present", "late"])))
    may_see_fees = (
        request.user.has_perm("fees.view_feeinvoice")
        or student.user_id == request.user.pk
        or (hasattr(request.user, "guardian_profile"))
    )
    invoices = FeeInvoice.objects.filter(student=student).with_totals() if may_see_fees else FeeInvoice.objects.none()
    published = Exam.objects.filter(
        school=request.school,
        status="published",
        academic_year__in=[e.academic_year_id for e in enrollments],
    ).order_by("-academic_year__start_date", "start_date")
    return render(
        request,
        "students/detail.html",
        {
            "student": student,
            "page_title": student.full_name,
            "enrollments": enrollments,
            "current": current,
            "guardians": student.guardian_links.select_related("guardian"),
            "documents": student.documents.all(),
            "invoices": invoices,
            "outstanding": sum((i.balance for i in invoices), start=0),
            "may_see_fees": may_see_fees,
            "attendance_total": summary["total"],
            "attendance_present": summary["present"],
            "attendance_pct": round(summary["present"] * 100 / summary["total"]) if summary["total"] else None,
            "recent_attendance": attendance.order_by("-date")[:14],
            "published_exams": published,
            "status_form": StatusChangeForm(),
        },
    )


@require_permission("students.add_guardian")
def guardian(request, student_pk):
    student = get_object_or_404(Student, school=request.school, pk=student_pk)
    form = GuardianForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        from django.db import transaction

        with transaction.atomic():
            obj = form.save()
            if form.cleaned_data["is_primary"]:
                StudentGuardian.objects.filter(student=student).update(is_primary=False)
            StudentGuardian.objects.create(
                student=student,
                guardian=obj,
                relation=form.cleaned_data["relation"],
                is_primary=form.cleaned_data["is_primary"],
            )
            audit(request, "guardian.created", obj, f"linked to {student}")
        return redirect("students:detail", pk=student.pk)
    return render(request, "generic/form.html", {"form": form, "page_title": f"Add guardian for {student.full_name}"})


@require_permission("students.change_student")
def leaving(request, pk):
    """Graduate, transfer out or withdraw a student."""
    student = get_object_or_404(students_for(request.user, request.school), pk=pk)
    form = StatusChangeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            change_status(
                school=request.school,
                user=request.user,
                student=student,
                status=form.cleaned_data["status"],
                effective_date=form.cleaned_data["effective_date"],
                reason=form.cleaned_data["reason"],
                force=form.cleaned_data["force"],
            )
            messages.success(request, f"{student.full_name} is now {form.cleaned_data['status']}.")
            return redirect("students:detail", pk=student.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "generic/form.html",
        {"form": form, "page_title": f"Leaving record for {student.full_name}", "submit_label": "Confirm"},
    )


@require_permission("students.add_studentdocument")
def document_upload(request, pk):
    student = get_object_or_404(Student, school=request.school, pk=pk)
    form = DocumentForm(request.POST or None, request.FILES or None, school=request.school)
    form.instance.student = student
    if request.method == "POST" and form.is_valid():
        form.instance.uploaded_by = request.user
        form.save()
        audit(request, "student_document.uploaded", form.instance, f"{form.instance.title} for {student}")
        return redirect("students:detail", pk=pk)
    return render(request, "generic/form.html", {"form": form, "page_title": "Upload document"})


@require_permission(None)
def document_download(request, pk):
    doc = get_object_or_404(
        StudentDocument, school=request.school, pk=pk, student__in=students_for(request.user, request.school)
    )
    return FileResponse(doc.file.open("rb"), as_attachment=True, filename=doc.file.name.rsplit("/", 1)[-1])


@require_permission("students.change_enrollment")
def promotion(request):
    form = PromotionForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        try:
            count = promote(request.school, **form.cleaned_data)
            audit(request, "students.promoted", description=f"{count} students")
            messages.success(request, f"Promoted {count} students; previous enrollments retained.")
            return redirect("students:list")
        except ValidationError as e:
            form.add_error(None, e)
    return render(request, "generic/form.html", {"form": form, "page_title": "Promote students"})


@require_permission("students.add_student")
def import_csv(request):
    """Two steps: show what the file will do, then write it."""
    form = ImportForm(request.POST or None, request.FILES or None)
    preview = None
    if request.method == "POST" and request.POST.get("confirm") and request.session.get(IMPORT_SESSION_KEY):
        # The rows the preview checked, handed straight to the import. Writing them back
        # out as CSV and parsing them again would be a second chance to disagree.
        rows = request.session.pop(IMPORT_SESSION_KEY)
        try:
            count = import_students(request.school, rows, user=request.user)
            messages.success(request, f"Imported {count} students.")
            return redirect("students:list")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
    elif request.method == "POST" and form.is_valid():
        try:
            rows = read_import_rows(form.cleaned_data["file"])
            prepared, errors = validate_import(request.school, rows)
            preview = {
                "rows": prepared[:20],
                "total": len(prepared),
                "errors": errors,
                "valid": not errors,
            }
            if not errors:
                request.session[IMPORT_SESSION_KEY] = rows
        except ValidationError as exc:
            form.add_error("file", exc)
    return render(request, "students/import.html", {"form": form, "preview": preview, "page_title": "Import students"})


@require_permission("students.view_student")
def export(request):
    students = (
        students_for(request.user, request.school)
        .prefetch_related("enrollments__section__class_level", "guardian_links__guardian")
        .order_by("student_id")
    )
    headers = [
        "ID",
        "Name",
        "Name (Bangla)",
        "Gender",
        "Date of birth",
        "Class",
        "Roll",
        "Guardian",
        "Guardian phone",
        "Admission date",
        "Status",
    ]
    rows = []
    for s in students:
        enrollment = s.current_enrollment
        guardian_obj = s.primary_guardian
        rows.append(
            [
                s.student_id,
                s.full_name,
                s.name_bn,
                s.get_gender_display(),
                s.date_of_birth,
                str(enrollment.section) if enrollment else "",
                enrollment.roll_number if enrollment else "",
                guardian_obj.full_name if guardian_obj else "",
                guardian_obj.phone if guardian_obj else "",
                s.admission_date,
                s.get_status_display(),
            ]
        )
    return spreadsheet("students", headers, rows, request.GET.get("format", "csv"))


@require_permission(None)
def id_card(request, pk):
    from .id_cards import id_card_pdf

    student = get_object_or_404(students_for(request.user, request.school), pk=pk)
    return id_card_pdf(request.school, [student])


@require_permission("students.view_student")
def id_cards(request):
    """Printable cards for a whole section."""
    from .id_cards import id_card_pdf

    section = get_object_or_404(Section, school=request.school, pk=request.GET.get("section", 0))
    if not request.user.has_perm("students.view_student"):
        raise PermissionDenied
    students = (
        students_for(request.user, request.school)
        .filter(enrollments__section=section, enrollments__academic_year__is_current=True)
        .distinct()
        .order_by("enrollments__roll_number")
    )
    return id_card_pdf(request.school, list(students), subtitle=str(section))


def scope_to_visible_students(view, queryset):
    """Enrollment lists follow the same visibility rule as the student roster."""
    return queryset.filter(student__in=students_for(view.request.user, view.request.school))
