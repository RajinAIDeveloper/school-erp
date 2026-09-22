"""Fee forms: generation, ad-hoc invoices, collection and the dues filter."""

from django import forms
from django.utils import timezone

from academics.models import AcademicYear, ClassLevel, Section
from core.forms import SchoolModelForm, TailwindFormMixin
from students.models import Enrollment, Student

from .models import MONTHS, FeeCategory, FeeInvoice, FeePayment


class GenerateInvoicesForm(TailwindFormMixin, forms.Form):
    academic_year = forms.ModelChoiceField(queryset=None)
    class_level = forms.ModelChoiceField(
        queryset=None, required=False, label="Class", help_text="Leave blank to bill every class at once."
    )
    month = forms.TypedChoiceField(choices=MONTHS, coerce=int)
    issue_date = forms.DateField(initial=timezone.localdate)
    due_date = forms.DateField()

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["academic_year"].queryset = AcademicYear.objects.filter(school=school)
        self.fields["class_level"].queryset = ClassLevel.objects.filter(school=school)
        current = AcademicYear.current_for(school)
        if current:
            self.fields["academic_year"].initial = current
        self.fields["month"].initial = timezone.localdate().month

    def clean(self):
        data = super().clean()
        if data.get("issue_date") and data.get("due_date") and data["due_date"] < data["issue_date"]:
            self.add_error("due_date", "Due date cannot precede issue date.")
        return data


class PaymentForm(SchoolModelForm):
    class Meta:
        model = FeePayment
        fields = ["amount", "method", "reference", "date"]

    def __init__(self, *args, invoice, **kwargs):
        self.invoice = invoice
        super().__init__(*args, **kwargs)
        self.fields["amount"].initial = invoice.balance
        self.fields["date"].initial = timezone.localdate()

    def clean(self):
        d = super().clean()
        if d.get("amount") is not None and (d["amount"] <= 0 or d["amount"] > self.invoice.balance):
            self.add_error("amount", "Payment must be positive and no greater than the outstanding balance.")
        if d.get("method") != "cash" and not d.get("reference"):
            self.add_error("reference", "Transaction reference is required.")
        if d.get("date") and d["date"] > timezone.localdate():
            self.add_error("date", "A receipt cannot be dated in the future.")
        return d


class InvoiceForm(TailwindFormMixin, forms.Form):
    """A one-off invoice. Fee lines come from the item formset alongside it."""

    student = forms.ModelChoiceField(queryset=Student.objects.none())
    issue_date = forms.DateField(initial=timezone.localdate)
    due_date = forms.DateField()
    month = forms.TypedChoiceField(
        choices=[("", "Not a monthly fee"), *MONTHS], coerce=int, required=False, empty_value=None
    )
    discount = forms.DecimalField(max_digits=12, decimal_places=2, initial=0, min_value=0)
    late_fee = forms.DecimalField(max_digits=12, decimal_places=2, initial=0, min_value=0)
    notes = forms.CharField(max_length=200, required=False)

    def __init__(self, *args, school, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)
        self.fields["student"].queryset = Student.objects.filter(school=school, status="active")

    def clean(self):
        data = super().clean()
        student = data.get("student")
        if not student:
            return data
        enrollment = (
            Enrollment.objects.filter(student=student, academic_year__is_current=True)
            .select_related("academic_year", "section")
            .first()
        )
        if enrollment is None:
            self.add_error("student", "This student has no enrollment in the current year to bill against.")
            return data
        data["enrollment"] = enrollment
        data["academic_year"] = enrollment.academic_year
        if data.get("month") and FeeInvoice.objects.filter(enrollment=enrollment, month=data["month"]).exists():
            self.add_error("month", "This student already has an invoice for that month.")
        if data.get("issue_date") and data.get("due_date") and data["due_date"] < data["issue_date"]:
            self.add_error("due_date", "Due date cannot precede the issue date.")
        return data


class InvoiceItemForm(TailwindFormMixin, forms.Form):
    category = forms.ModelChoiceField(queryset=FeeCategory.objects.none(), required=False)
    description = forms.CharField(max_length=150, required=False)
    amount = forms.DecimalField(max_digits=12, decimal_places=2, required=False, min_value=0)

    def __init__(self, *args, school=None, **kwargs):
        super().__init__(*args, **kwargs)
        if school is not None:
            self.fields["category"].queryset = FeeCategory.objects.filter(school=school, is_active=True)

    def clean(self):
        data = super().clean()
        if data.get("amount") and not data.get("category"):
            self.add_error("category", "Choose a fee head for this amount.")
        return data


class BaseInvoiceItemFormSet(forms.BaseFormSet):
    def __init__(self, *args, school=None, **kwargs):
        self.school = school
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["school"] = self.school
        return kwargs

    def lines(self):
        return [
            (form.cleaned_data["category"], form.cleaned_data["amount"])
            for form in self.forms
            if form.cleaned_data.get("category") and form.cleaned_data.get("amount")
        ]


InvoiceItemFormSet = forms.formset_factory(
    InvoiceItemForm, formset=BaseInvoiceItemFormSet, extra=4, max_num=20, validate_max=True
)


class DuesFilterForm(TailwindFormMixin, forms.Form):
    class_level = forms.ModelChoiceField(queryset=None, required=False, label="Class")
    section = forms.ModelChoiceField(
        queryset=None,
        required=False,
        widget=forms.Select(
            attrs={"data-depends-on": "#id_class_level", "data-url": "/academics/sections.json?class_level="}
        ),
    )
    as_of = forms.DateField(required=False, initial=timezone.localdate, label="As of")
    overdue_only = forms.BooleanField(required=False, initial=False)

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["class_level"].queryset = ClassLevel.objects.filter(school=school)
        self.fields["section"].queryset = Section.objects.filter(school=school).select_related("class_level")


class LateFeeForm(TailwindFormMixin, forms.Form):
    as_of = forms.DateField(initial=timezone.localdate, label="Charge as of")
    cap = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        min_value=0,
        help_text="Leave blank to use the cap from Basic settings.",
    )
