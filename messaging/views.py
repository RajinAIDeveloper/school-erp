from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import require_permission
from core.forms import TailwindFormMixin
from core.models import audit

from .models import SMSBatch, SMSMessage
from .services import queue_batch


class ComposeForm(TailwindFormMixin, forms.Form):
    title = forms.CharField(max_length=150)
    recipients = forms.ChoiceField(choices=SMSBatch.Recipients.choices)
    section = forms.ModelChoiceField(queryset=None, required=False)
    numbers = forms.CharField(
        widget=forms.Textarea, required=False, help_text="One Bangladeshi mobile number per line."
    )
    body = forms.CharField(max_length=480, widget=forms.Textarea)

    def __init__(self, *args, school, **kwargs):
        from academics.models import Section

        super().__init__(*args, **kwargs)
        self.fields["section"].queryset = Section.objects.filter(school=school)

    def clean(self):
        data = super().clean()
        if data.get("recipients") == "class" and not data.get("section"):
            self.add_error("section", "Select a section.")
        if data.get("recipients") == "custom" and not data.get("numbers"):
            self.add_error("numbers", "Enter at least one recipient.")
        return data


@require_permission("messaging.view_smsmessage")
def listing(request):
    rows = SMSMessage.objects.filter(school=request.school).select_related("batch")
    return render(request, "messaging/list.html", {"rows": rows[:200], "page_title": "SMS queue and delivery log"})


@require_permission("messaging.add_smsmessage")
def compose(request):
    from django.core.exceptions import ValidationError

    from employees.models import Employee
    from students.models import Guardian

    form = ComposeForm(request.POST or None, school=request.school)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        if d["recipients"] == "custom":
            contacts = [("", v) for v in d["numbers"].splitlines() if v.strip()]
        elif d["recipients"] in ("all_staff", "teachers"):
            qs = Employee.objects.filter(school=request.school, status="active")
            if d["recipients"] == "teachers":
                qs = qs.filter(employee_type="teacher")
            contacts = [(e.full_name, e.phone) for e in qs]
        else:
            qs = Guardian.objects.filter(school=request.school)
            if d["recipients"] == "class":
                qs = qs.filter(
                    student_links__student__enrollments__section=d["section"],
                    student_links__student__enrollments__academic_year__is_current=True,
                ).distinct()
            contacts = list(qs.values_list("full_name", "phone"))
        try:
            batch = queue_batch(request.school, d["title"], d["recipients"], d["body"], contacts, request.user)
            audit(request, "sms.queued", batch)
            messages.success(request, f"Queued {batch.total} messages.")
            return redirect("messaging:batch_list")
        except ValidationError as e:
            form.add_error(None, e)
    return render(request, "generic/form.html", {"form": form, "page_title": "Compose SMS"})


@require_permission("messaging.add_smsmessage")
@require_POST
def retry(request, pk):
    obj = get_object_or_404(SMSMessage, school=request.school, pk=pk, status="failed")
    obj.status = "queued"
    obj.provider_response = ""
    obj.save(update_fields=["status", "provider_response", "updated_at"])
    audit(request, "sms.requeued", obj)
    return redirect("messaging:batch_list")
