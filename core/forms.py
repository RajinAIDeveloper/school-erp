"""
Base forms. SchoolModelForm limits every ModelChoiceField / ModelMultipleChoiceField
to the current school and applies Tailwind CSS classes to widgets.
"""

from django import forms
from django.contrib.auth import get_user_model

from .models import School

INPUT_CLASS = (
    "mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm "
    "shadow-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 "
    "disabled:bg-slate-100"
)
CHECKBOX_CLASS = "h-4 w-4 rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
FILE_CLASS = (
    "mt-1 block w-full text-sm text-slate-600 file:mr-4 file:rounded file:border-0 "
    "file:bg-indigo-50 file:px-3 file:py-2 file:text-indigo-700"
)


def tailwindify(form):
    for field in form.fields.values():
        w = field.widget
        if isinstance(w, forms.CheckboxInput):
            w.attrs.setdefault("class", CHECKBOX_CLASS)
        elif isinstance(w, (forms.ClearableFileInput, forms.FileInput)):
            w.attrs.setdefault("class", FILE_CLASS)
        elif isinstance(w, forms.CheckboxSelectMultiple):
            w.attrs.setdefault("class", "space-y-1")
        else:
            existing = w.attrs.get("class", "")
            w.attrs["class"] = f"{existing} {INPUT_CLASS}".strip()
        if isinstance(w, forms.DateInput):
            w.input_type = "date"
            w.format = "%Y-%m-%d"
        if isinstance(w, forms.TimeInput):
            w.input_type = "time"
            w.format = "%H:%M"


class TailwindFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tailwindify(self)


class SchoolModelForm(TailwindFormMixin, forms.ModelForm):
    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)
        if school is not None and hasattr(self.instance, "school_id"):
            self.instance.school = school
        if school is None:
            return
        User = get_user_model()
        for field in self.fields.values():
            if isinstance(field, (forms.ModelChoiceField, forms.ModelMultipleChoiceField)):
                model = field.queryset.model
                if model is School:
                    field.queryset = field.queryset.filter(pk=school.pk)
                elif model is User:
                    field.queryset = field.queryset.filter(school=school)
                elif any(f.name == "school" for f in model._meta.fields):
                    field.queryset = field.queryset.filter(school=school)

    def clean(self):
        cleaned = super().clean()
        for name, value in cleaned.items():
            if (
                name in {"amount", "basic_salary", "fixed_amount", "full_marks", "pass_marks"}
                and value is not None
                and value < 0
            ):
                self.add_error(name, "Must not be negative.")
        section = cleaned.get("section")
        class_level = cleaned.get("class_level")
        if section and class_level and section.class_level_id != class_level.pk:
            self.add_error("section", "Section must belong to the selected class.")
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            self.add_error("end_date", "End date must not precede start date.")
        start_time, end_time = cleaned.get("start_time"), cleaned.get("end_time")
        if start_time and end_time and end_time <= start_time:
            self.add_error("end_time", "End time must follow start time.")
        upload = cleaned.get("file")
        if upload and hasattr(upload, "content_type"):
            from pathlib import Path

            if upload.size > 10 * 1024 * 1024:
                self.add_error("file", "Maximum upload size is 10 MB.")
            if Path(upload.name).suffix.lower() not in {
                ".pdf",
                ".png",
                ".jpg",
                ".jpeg",
                ".docx",
                ".xlsx",
                ".csv",
                ".txt",
            }:
                self.add_error("file", "Allowed files: PDF, images, DOCX, XLSX, CSV and text.")
        if "percent" in cleaned and cleaned["percent"] is not None and not 0 <= cleaned["percent"] <= 100:
            self.add_error("percent", "Percentage must be between 0 and 100.")
        income = cleaned.get("income_account")
        if income and income.account_type != "income":
            self.add_error("income_account", "Choose an income account.")
        parent = cleaned.get("parent")
        seen = {self.instance.pk} if self.instance.pk else set()
        while parent:
            if parent.pk in seen:
                self.add_error("parent", "An account hierarchy cannot contain a cycle.")
                break
            seen.add(parent.pk)
            parent = parent.parent
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        if hasattr(obj, "school_id") and not obj.school_id and self.school is not None:
            obj.school = self.school
        if commit:
            obj.save()
            self.save_m2m()
        return obj

    def _post_clean(self):
        from django.core.exceptions import ValidationError

        super()._post_clean()
        if self.errors:
            return
        exclude = self._get_validation_exclusions()
        exclude.discard("school")
        try:
            self.instance.validate_constraints(exclude=exclude)
        except ValidationError as exc:
            self.add_error(None, " ".join(exc.messages))


class SchoolForm(TailwindFormMixin, forms.ModelForm):
    class Meta:
        model = School
        fields = [
            "name",
            "short_name",
            "eiin",
            "motto",
            "address",
            "phone",
            "email",
            "website",
            "logo",
            "principal_name",
            "currency",
            "currency_symbol",
            "country",
            "timezone",
            "weekend_days",
            "assessment_system",
            "bangla_enabled",
            "default_language",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}


class SMSSettingsForm(TailwindFormMixin, forms.ModelForm):
    class Meta:
        model = School
        fields = ["sms_sender_id", "sms_api_url", "sms_api_key", "sms_extra_params"]


class NotificationSettingsForm(TailwindFormMixin, forms.ModelForm):
    """
    Which events text a family.

    Every switch is off until a school turns it on: each message costs the school money
    and lands on a parent's phone, so the default is silence.
    """

    class Meta:
        model = School
        fields = [
            "notify_absence_sms",
            "notify_payment_sms",
            "notify_due_sms",
            "notify_results_sms",
            "notify_admission_sms",
        ]


class FinancePolicyForm(TailwindFormMixin, forms.ModelForm):
    class Meta:
        model = School
        fields = [
            "late_fee_per_day",
            "late_fee_cap",
            "sibling_discount_percent",
            "books_locked_until",
            "staff_self_checkin",
        ]
        widgets = {"sms_api_key": forms.PasswordInput(render_value=True)}
