from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from core.access import require_permission, students_for
from core.exports import spreadsheet, pdf_response
from core.generic import ERPListView, ERPCreateView, ERPUpdateView
from core.models import audit
from .forms import StudentForm, GuardianForm, DocumentForm, PromotionForm
from .models import Student, StudentGuardian, StudentDocument
from .services import promote, import_students

class StudentListView(ERPListView):
    model = Student
    permission_required = "students.view_student"
    page_title = "Students"
    search_fields = ("first_name", "last_name", "student_id", "phone")
    columns = (("ID", "student_id"), ("Name", "full_name"), ("Gender", "get_gender_display"), ("Status", "status", "badge"))
    create_url_name = "students:create"
    update_url_name = "students:update"
    detail_url_name = "students:detail"
    extra_actions = (("Enrollments", "students:enrollment_list"), ("Promote", "students:promote"), ("Import CSV", "students:import"))
    def get_queryset(self):
        return super().get_queryset().filter(pk__in=students_for(self.request.user, self.request.school))

class StudentCreateView(ERPCreateView):
    model = Student
    form_class = StudentForm
    permission_required = "students.add_student"
    success_url_name = "students:list"
    page_title = "Admit student"
    def get_initial(self):
        return {"student_id": Student.next_student_id(self.request.school, str(timezone.localdate().year)), "admission_date": timezone.localdate()}

class StudentUpdateView(ERPUpdateView):
    model = Student
    form_class = StudentForm
    permission_required = "students.change_student"
    success_url_name = "students:list"
    page_title = "Edit student"

@require_permission(None)
def detail(request, pk):
    student = get_object_or_404(students_for(request.user, request.school), pk=pk)
    return render(request, "students/detail.html", {"student": student, "page_title": student.full_name,
        "enrollments": student.enrollments.select_related("section", "academic_year"),
        "guardians": student.guardian_links.select_related("guardian"), "documents": student.documents.all(),
        "invoices": student.invoices.all()})

@require_permission("students.add_guardian")
def guardian(request, student_pk):
    student = get_object_or_404(Student, school=request.school, pk=student_pk)
    form = GuardianForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            obj = form.save()
            if form.cleaned_data["is_primary"]:
                StudentGuardian.objects.filter(student=student).update(is_primary=False)
            StudentGuardian.objects.create(student=student, guardian=obj, relation=form.cleaned_data["relation"], is_primary=form.cleaned_data["is_primary"])
            audit(request, "guardian.created", obj)
        return redirect("students:detail", pk=student.pk)
    return render(request, "generic/form.html", {"form": form, "page_title": "Add guardian"})

@require_permission("students.add_studentdocument")
def document_upload(request, pk):
    student = get_object_or_404(Student, school=request.school, pk=pk)
    form = DocumentForm(request.POST or None, request.FILES or None, school=request.school)
    form.instance.student = student
    if request.method == "POST" and form.is_valid():
        form.instance.uploaded_by = request.user
        form.save()
        return redirect("students:detail", pk=pk)
    return render(request, "generic/form.html", {"form": form, "page_title": "Upload document"})

@require_permission(None)
def document_download(request, pk):
    doc = get_object_or_404(StudentDocument, school=request.school, pk=pk, student__in=students_for(request.user, request.school))
    return FileResponse(doc.file.open("rb"), as_attachment=True, filename=doc.file.name.rsplit("/",1)[-1])

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
    from django import forms
    from core.forms import TailwindFormMixin
    class UploadForm(TailwindFormMixin, forms.Form):
        file = forms.FileField(help_text="UTF-8 CSV: student_id, first_name, last_name, gender (M/F/O), date_of_birth, admission_date, status.")
    form = UploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            count = import_students(request.school, form.cleaned_data["file"])
            audit(request, "students.imported", description=str(count))
            messages.success(request, f"Imported {count} students.")
            return redirect("students:list")
        except ValidationError as e:
            form.add_error(None, e)
    return render(request, "generic/form.html", {"form": form, "page_title": "Import students"})

@require_permission("students.view_student")
def export(request):
    students = students_for(request.user, request.school)
    return spreadsheet("students", ["ID", "Name", "Status"], [[s.student_id, s.full_name, s.status] for s in students], request.GET.get("format", "csv"))

@require_permission(None)
def id_card(request, pk):
    s = get_object_or_404(students_for(request.user, request.school), pk=pk)
    return pdf_response("Student ID card", ["School", "ID", "Student", "Class"], [[request.school.name, s.student_id, s.full_name, str(s.current_enrollment.section) if s.current_enrollment else ""]])
