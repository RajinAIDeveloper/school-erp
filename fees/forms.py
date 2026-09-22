from django import forms
from django.utils import timezone
from academics.models import AcademicYear, ClassLevel
from core.forms import SchoolModelForm, TailwindFormMixin
from .models import FeePayment, FeeCategory, MONTHS

class GenerateInvoicesForm(TailwindFormMixin, forms.Form):
    academic_year=forms.ModelChoiceField(queryset=None)
    class_level=forms.ModelChoiceField(queryset=None)
    month=forms.TypedChoiceField(choices=MONTHS,coerce=int)
    issue_date=forms.DateField(initial=timezone.localdate)
    due_date=forms.DateField()
    def __init__(self,*args,school,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["academic_year"].queryset=AcademicYear.objects.filter(school=school)
        self.fields["class_level"].queryset=ClassLevel.objects.filter(school=school)
    def clean(self):
        d=super().clean()
        if d.get("issue_date") and d.get("due_date") and d["due_date"]<d["issue_date"]:
            self.add_error("due_date","Due date cannot precede issue date.")
        return d

class PaymentForm(SchoolModelForm):
    class Meta:
        model=FeePayment
        fields=["amount","method","reference","date"]
    def __init__(self,*args,invoice,**kwargs):
        self.invoice=invoice
        super().__init__(*args,**kwargs)
        self.fields["amount"].initial=invoice.balance
        self.fields["date"].initial=timezone.localdate()
    def clean(self):
        d=super().clean()
        if d.get("amount") is not None and (d["amount"]<=0 or d["amount"]>self.invoice.balance):
            self.add_error("amount","Payment must be positive and no greater than the outstanding balance.")
        if d.get("method")!="cash" and not d.get("reference"):
            self.add_error("reference","Transaction reference is required.")
        return d
