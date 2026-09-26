"""Optional dated teaching plans for a class subject."""

from django import forms

from core.forms import SchoolModelForm, TailwindFormMixin

from employees.models import Employee

from .models import AcademicYear, ClassSubject, Section, Subject, SubjectTeacher, TeachingPlanItem, Term


class SubjectAssignmentForm(TailwindFormMixin, forms.Form):
    """One screen for adding a subject to a class plan and assigning its teacher."""

    academic_year = forms.ModelChoiceField(queryset=AcademicYear.objects.none(), label="Academic year")
    section = forms.ModelChoiceField(queryset=Section.objects.none(), label="Class and section")
    subject = forms.ModelChoiceField(queryset=Subject.objects.none())
    teacher = forms.ModelChoiceField(queryset=Employee.objects.none())
    kind = forms.ChoiceField(choices=ClassSubject.Kind.choices, label="Subject type", required=False)

    def __init__(self, *args, school, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(school=school)
        self.fields["section"].queryset = Section.objects.filter(school=school, is_active=True).select_related("class_level")
        self.fields["subject"].queryset = Subject.objects.filter(school=school, is_active=True)
        self.fields["teacher"].queryset = Employee.objects.filter(
            school=school, employee_type=Employee.Type.TEACHER, status=Employee.Status.ACTIVE
        )
        self.fields["kind"].initial = ClassSubject.Kind.COMPULSORY
        self.fields["kind"].help_text = "Used only if this subject must be added to the class plan."

    def clean(self):
        cleaned = super().clean()
        year, section, subject, teacher = (
            cleaned.get("academic_year"), cleaned.get("section"), cleaned.get("subject"), cleaned.get("teacher")
        )
        if year and section and subject and teacher and SubjectTeacher.objects.filter(
            school=self.school, academic_year=year, section=section, subject=subject, teacher=teacher
        ).exists():
            self.add_error("teacher", "This teacher already teaches this subject in this section for this year.")
        cleaned["kind"] = cleaned.get("kind") or ClassSubject.Kind.COMPULSORY
        return cleaned


class TeachingPlanItemForm(SchoolModelForm):
    class Meta:
        model = TeachingPlanItem
        fields = ["term", "section", "planned_date", "kind", "unit", "topic", "learning_goal", "taught_on"]
        widgets = {"learning_goal": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, subject_row, allowed_sections, may_plan_all, **kwargs):
        self.subject_row = subject_row
        self.allowed_sections = allowed_sections
        self.may_plan_all = may_plan_all
        super().__init__(*args, **kwargs)
        self.instance.academic_year = subject_row.academic_year
        self.instance.class_level = subject_row.class_level
        self.instance.subject = subject_row.subject
        self.fields["term"].queryset = Term.objects.filter(
            school=self.school, academic_year=subject_row.academic_year
        )
        self.fields["section"].queryset = Section.objects.filter(pk__in=allowed_sections.values("pk"))
        if not may_plan_all:
            self.fields["section"].required = True
            self.fields["section"].help_text = "Choose a section you teach."
            if allowed_sections.count() == 1 and not self.is_bound and not self.instance.pk:
                self.initial["section"] = allowed_sections.first().pk
        self.fields["planned_date"].help_text = "The date you expect to teach this topic or hold the activity."
        self.fields["kind"].help_text = (
            "Quiz and class test here are planning notes. Create an exam paper separately to enter marks."
        )
        if not self.instance.pk:
            self.fields.pop("taught_on")
