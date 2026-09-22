from django import forms

from core.forms import SchoolModelForm, TailwindFormMixin

from .models import Enrollment, Guardian, Student, StudentDocument, StudentGuardian


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


class DocumentForm(SchoolModelForm):
    class Meta:
        model = StudentDocument
        fields = ["title", "file"]


class PromotionForm(TailwindFormMixin, forms.Form):
    source_year = forms.ModelChoiceField(queryset=None)
    source_section = forms.ModelChoiceField(queryset=None)
    target_year = forms.ModelChoiceField(queryset=None)
    target_section = forms.ModelChoiceField(queryset=None)

    def __init__(self, *args, school, **kwargs):
        from academics.models import AcademicYear, Section

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
