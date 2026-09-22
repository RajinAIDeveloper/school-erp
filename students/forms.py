"""Student, guardian, enrollment and admission forms."""

from django import forms
from django.utils import timezone

from academics.models import AcademicYear, ClassLevel, Section
from core.forms import SchoolModelForm, TailwindFormMixin

from .models import Enrollment, Guardian, Student, StudentDocument, StudentGuardian


def section_widget():
    """A section select that reloads itself when the class changes."""
    return forms.Select(
        attrs={"data-depends-on": "#id_class_level", "data-url": "/academics/sections.json?class_level="}
    )


class StudentForm(SchoolModelForm):
    class Meta:
        model = Student
        fields = [
            "student_id",
            "user",
            "first_name",
            "last_name",
            "name_bn",
            "gender",
            "date_of_birth",
            "birth_registration_no",
            "blood_group",
            "religion",
            "phone",
            "email",
            "photo",
            "present_address",
            "permanent_address",
            "previous_school",
            "admission_date",
            "status",
            "notes",
        ]


class GuardianForm(SchoolModelForm):
    relation = forms.ChoiceField(choices=StudentGuardian.Relation.choices)
    is_primary = forms.BooleanField(required=False)

    class Meta:
        model = Guardian
        fields = ["full_name", "user", "phone", "email", "nid", "occupation", "address"]


class EnrollmentForm(SchoolModelForm):
    class Meta:
        model = Enrollment
        fields = ["student", "academic_year", "class_level", "section", "roll_number", "status"]
        widgets = {"section": section_widget()}


class DocumentForm(SchoolModelForm):
    class Meta:
        model = StudentDocument
        fields = ["title", "file"]


class AdmissionForm(SchoolModelForm):
    """
    One screen for the whole admission: the child, the guardian who will be contacted,
    and the class they join. Office staff should never have to remember a three-screen
    sequence to get a student onto a roll.
    """

    guardian_name = forms.CharField(max_length=200, label="Guardian name")
    guardian_phone = forms.CharField(max_length=25, label="Guardian mobile", help_text="e.g. 017XXXXXXXX")
    guardian_relation = forms.ChoiceField(
        choices=StudentGuardian.Relation.choices, initial=StudentGuardian.Relation.FATHER, label="Relation"
    )
    guardian_nid = forms.CharField(max_length=20, required=False, label="Guardian NID")
    guardian_email = forms.EmailField(required=False, label="Guardian email")
    guardian_occupation = forms.CharField(max_length=100, required=False, label="Guardian occupation")

    academic_year = forms.ModelChoiceField(queryset=AcademicYear.objects.none())
    class_level = forms.ModelChoiceField(queryset=ClassLevel.objects.none(), label="Class")
    section = forms.ModelChoiceField(queryset=Section.objects.none(), widget=section_widget())
    roll_number = forms.IntegerField(min_value=1, required=False, help_text="Left blank, the next free roll is used.")

    field_groups = (
        (
            "Student",
            [
                "student_id",
                "first_name",
                "last_name",
                "name_bn",
                "gender",
                "date_of_birth",
                "birth_registration_no",
                "blood_group",
                "religion",
                "photo",
            ],
        ),
        ("Contact", ["phone", "email", "present_address", "permanent_address", "previous_school"]),
        (
            "Guardian",
            [
                "guardian_name",
                "guardian_relation",
                "guardian_phone",
                "guardian_nid",
                "guardian_email",
                "guardian_occupation",
            ],
        ),
        ("Enrollment", ["academic_year", "class_level", "section", "roll_number", "admission_date"]),
    )

    class Meta:
        model = Student
        fields = [
            "student_id",
            "first_name",
            "last_name",
            "name_bn",
            "gender",
            "date_of_birth",
            "birth_registration_no",
            "blood_group",
            "religion",
            "photo",
            "phone",
            "email",
            "present_address",
            "permanent_address",
            "previous_school",
            "admission_date",
        ]

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, school=school, **kwargs)
        if school is None:
            return
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(school=school)
        self.fields["class_level"].queryset = ClassLevel.objects.filter(school=school)
        self.fields["section"].queryset = Section.objects.filter(school=school)
        current = AcademicYear.current_for(school)
        if current:
            self.fields["academic_year"].initial = current
        self.fields["admission_date"].initial = timezone.localdate()
        self.fields["student_id"].initial = Student.next_student_id(
            school, current.name if current else str(timezone.localdate().year)
        )

    def clean(self):
        cleaned = super().clean()
        section, class_level = cleaned.get("section"), cleaned.get("class_level")
        if section and class_level and section.class_level_id != class_level.pk:
            self.add_error("section", "Section must belong to the selected class.")
        year, section, roll = cleaned.get("academic_year"), cleaned.get("section"), cleaned.get("roll_number")
        if year and section and roll:
            taken = Enrollment.objects.filter(academic_year=year, section=section, roll_number=roll).exists()
            if taken:
                self.add_error("roll_number", f"Roll {roll} is already used in {section} this year.")
        return cleaned

    def groups(self):
        """Yield (legend, [bound fields]) so the template can render labelled sections."""
        for legend, names in self.field_groups:
            yield legend, [self[name] for name in names if name in self.fields]


class PromotionForm(TailwindFormMixin, forms.Form):
    source_year = forms.ModelChoiceField(queryset=None)
    source_section = forms.ModelChoiceField(queryset=None)
    target_year = forms.ModelChoiceField(queryset=None)
    target_section = forms.ModelChoiceField(queryset=None)

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        for key in ("source_year", "target_year"):
            self.fields[key].queryset = AcademicYear.objects.filter(school=school)
        for key in ("source_section", "target_section"):
            self.fields[key].queryset = Section.objects.filter(school=school)

    def clean(self):
        data = super().clean()
        if (
            data.get("source_year")
            and data.get("target_year")
            and data["target_year"].start_date <= data["source_year"].start_date
        ):
            raise forms.ValidationError("Target year must start after source year.")
        return data


class StatusChangeForm(TailwindFormMixin, forms.Form):
    """Graduate, transfer out or withdraw a student, closing their current enrollment."""

    status = forms.ChoiceField(
        choices=[c for c in Student.Status.choices if c[0] != Student.Status.ACTIVE], label="New status"
    )
    effective_date = forms.DateField(initial=timezone.localdate)
    reason = forms.CharField(max_length=200, widget=forms.Textarea(attrs={"rows": 2}), required=False)
    force = forms.BooleanField(
        required=False,
        label="Leave anyway despite unpaid fees",
        help_text="Only an administrator may release a student who still owes fees.",
    )


class ImportForm(TailwindFormMixin, forms.Form):
    file = forms.FileField(
        label="CSV file",
        help_text="UTF-8 CSV. Required: student_id, first_name, gender (M/F/O), date_of_birth, admission_date. "
        "Optional: last_name, name_bn, phone, religion, blood_group, present_address, guardian_name, "
        "guardian_phone, guardian_relation, class_level, section, roll_number.",
    )
