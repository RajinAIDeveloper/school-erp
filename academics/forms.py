"""Optional dated teaching plans for a class subject."""

from django import forms

from core.forms import SchoolModelForm

from .models import Section, TeachingPlanItem, Term


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
