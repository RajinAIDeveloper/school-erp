"""SMS: composing, the outbox and delivery reporting."""

import json

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from academics.models import ClassLevel, Section
from core.access import require_permission
from core.exports import spreadsheet
from core.forms import TailwindFormMixin
from core.generic import ERPListView
from core.models import audit

from .models import SMSBatch, SMSMessage, SMSTemplate
from .services import describe_length, queue_batch, render_body, segments


class ComposeForm(TailwindFormMixin, forms.Form):
    title = forms.CharField(max_length=150, help_text="For your own records; recipients never see it.")
    recipients = forms.ChoiceField(choices=SMSBatch.Recipients.choices)
    class_level = forms.ModelChoiceField(queryset=ClassLevel.objects.none(), required=False, label="Class")
    section = forms.ModelChoiceField(
        queryset=Section.objects.none(),
        required=False,
        widget=forms.Select(
            attrs={"data-depends-on": "#id_class_level", "data-url": "/academics/sections.json?class_level="}
        ),
    )
    template = forms.ModelChoiceField(
        queryset=SMSTemplate.objects.none(),
        required=False,
        help_text="Pick one to fill the message below, then edit it freely.",
    )
    numbers = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4}),
        required=False,
        help_text="One Bangladeshi mobile number per line.",
    )
    body = forms.CharField(
        max_length=480,
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Placeholders: {student} {class} {school} {amount} {date}",
    )

    def __init__(self, *args, school, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["class_level"].queryset = ClassLevel.objects.filter(school=school)
        self.fields["section"].queryset = Section.objects.filter(school=school).select_related("class_level")
        self.fields["template"].queryset = SMSTemplate.objects.filter(school=school)

    def clean(self):
        data = super().clean()
        kind = data.get("recipients")
        if kind == SMSBatch.Recipients.CLASS and not (data.get("section") or data.get("class_level")):
            self.add_error("section", "Choose a class or a section.")
        if kind == SMSBatch.Recipients.CUSTOM and not data.get("numbers"):
            self.add_error("numbers", "Enter at least one recipient.")
        return data


def resolve_contacts(school, data):
    """
    Work out who a batch goes to, with the context each message needs.

    Returns (contacts, description) where a contact is (name, phone, context).
    """
    from employees.models import Employee
    from students.models import Student

    kind = data["recipients"]
    school_name = school.short_name or school.name

    if kind == SMSBatch.Recipients.CUSTOM:
        numbers = [line.strip() for line in data["numbers"].splitlines() if line.strip()]
        return [("", number, {"school": school_name}) for number in numbers], f"{len(numbers)} number(s)"

    if kind in (SMSBatch.Recipients.ALL_STAFF, SMSBatch.Recipients.TEACHERS):
        staff = Employee.objects.filter(school=school, status=Employee.Status.ACTIVE)
        if kind == SMSBatch.Recipients.TEACHERS:
            staff = staff.filter(employee_type=Employee.Type.TEACHER)
        contacts = [(e.full_name, e.phone, {"student": e.full_name, "school": school_name}) for e in staff if e.phone]
        return contacts, f"{len(contacts)} staff member(s)"

    # Guardians, either of one class/section or of the whole school. One message per
    # child, so a family with two children hears about both by name.
    students = Student.objects.filter(school=school, status=Student.Status.ACTIVE)
    if kind == SMSBatch.Recipients.CLASS:
        if data.get("section"):
            students = students.filter(
                enrollments__section=data["section"], enrollments__academic_year__is_current=True
            )
        else:
            students = students.filter(
                enrollments__class_level=data["class_level"], enrollments__academic_year__is_current=True
            )
    students = students.prefetch_related("guardian_links__guardian").distinct()

    contacts = []
    for student in students:
        guardian = student.primary_guardian
        if guardian is None or not guardian.phone or not guardian.sms_opt_in:
            continue
        enrollment = student.current_enrollment
        contacts.append(
            (
                guardian.full_name,
                guardian.phone,
                {
                    "student": student.full_name,
                    "class": str(enrollment.section) if enrollment else "",
                    "school": school_name,
                },
            )
        )
    return contacts, f"{len(contacts)} guardian(s)"


class BatchListView(ERPListView):
    model = SMSBatch
    permission_required = "messaging.view_smsmessage"
    page_title = "SMS batches"
    search_fields = ("title", "body")
    select_related = ("sent_by",)
    columns = (
        ("Title", "title"),
        ("Recipients", "get_recipients_display"),
        ("Sent", "sent_count"),
        ("Queued", "queued_count"),
        ("Failed", "failed_count"),
        ("By", "sent_by"),
        ("When", "created_at", "datetime"),
    )
    detail_url_name = "messaging:batch_detail"
    extra_actions = (
        ("Compose", "messaging:compose", "messaging.add_smsmessage"),
        ("Outbox", "messaging:message_list", "messaging.view_smsmessage"),
        ("Templates", "messaging:template_list", "messaging.view_smstemplate"),
    )


@require_permission("messaging.view_smsmessage")
def batch_detail(request, pk):
    batch = get_object_or_404(SMSBatch.objects.select_related("sent_by"), school=request.school, pk=pk)
    return render(
        request,
        "messaging/batch_detail.html",
        {
            "batch": batch,
            "rows": batch.messages.order_by("status", "recipient_name"),
            "parts": segments(batch.body),
            "page_title": batch.title,
        },
    )


@require_permission("messaging.view_smsmessage")
def message_list(request):
    """The outbox: every message, newest first, filtered by delivery state."""
    rows = SMSMessage.objects.filter(school=request.school).select_related("batch")
    status = request.GET.get("status", "")
    if status in dict(SMSMessage.Status.choices):
        rows = rows.filter(status=status)
    query = request.GET.get("q", "").strip()
    if query:
        rows = rows.filter(phone__icontains=query)
    if request.GET.get("format") in ("csv", "xlsx"):
        return spreadsheet(
            "sms-outbox",
            ["Recipient", "Phone", "Message", "Status", "Attempts", "Sent at", "Response"],
            [
                [m.recipient_name, m.phone, m.body, m.status, m.attempts, m.sent_at, m.provider_response[:200]]
                for m in rows[:5000]
            ],
            request.GET["format"],
        )
    counts = {
        value: SMSMessage.objects.filter(school=request.school, status=value).count()
        for value, _label in SMSMessage.Status.choices
    }
    return render(
        request,
        "messaging/list.html",
        {
            "rows": rows[:300],
            "status": status,
            "q": query,
            "statuses": SMSMessage.Status.choices,
            "counts": counts,
            "page_title": "SMS outbox",
        },
    )


@require_permission("messaging.add_smsmessage")
def compose(request):
    form = ComposeForm(request.POST or None, school=request.school)
    preview = None
    if request.method == "POST" and form.is_valid():
        contacts, description = resolve_contacts(request.school, form.cleaned_data)
        body = form.cleaned_data["body"]
        if request.POST.get("preview"):
            sample = contacts[0] if contacts else None
            preview = {
                "count": len(contacts),
                "description": description,
                "length": describe_length(body),
                "parts": segments(body),
                "sample_name": sample[0] if sample else "",
                "sample_phone": sample[1] if sample else "",
                "sample_body": render_body(body, sample[2]) if sample else body,
            }
        else:
            try:
                batch = queue_batch(
                    request.school,
                    form.cleaned_data["title"],
                    form.cleaned_data["recipients"],
                    body,
                    contacts,
                    request.user,
                )
                audit(request, "sms.queued", batch, f"{batch.total} message(s)")
                messages.success(
                    request,
                    f"Queued {batch.total} message(s). The worker sends them; nothing has left the school yet.",
                )
                return redirect("messaging:batch_detail", pk=batch.pk)
            except ValidationError as exc:
                form.add_error(None, exc)
    return render(
        request,
        "messaging/compose.html",
        {
            "form": form,
            "preview": preview,
            # Bodies as JSON so choosing a template fills the box without a round trip.
            "template_bodies": json.dumps(
                {str(t.pk): t.body for t in SMSTemplate.objects.filter(school=request.school)}
            ),
            "page_title": "Compose SMS",
        },
    )


@require_permission("messaging.add_smsmessage")
@require_POST
def retry(request, pk):
    message = get_object_or_404(SMSMessage, school=request.school, pk=pk, status=SMSMessage.Status.FAILED)
    message.status = SMSMessage.Status.QUEUED
    message.provider_response = ""
    message.attempts = 0
    message.save(update_fields=["status", "provider_response", "attempts", "updated_at"])
    audit(request, "sms.requeued", message)
    messages.success(request, "Message put back in the queue.")
    return redirect(request.POST.get("next") or "messaging:message_list")


@require_permission("messaging.add_smsmessage")
@require_POST
def retry_batch(request, pk):
    batch = get_object_or_404(SMSBatch, school=request.school, pk=pk)
    updated = batch.messages.filter(status=SMSMessage.Status.FAILED).update(
        status=SMSMessage.Status.QUEUED, provider_response="", attempts=0
    )
    audit(request, "sms.requeued", batch, f"{updated} message(s)")
    messages.success(request, f"{updated} failed message(s) put back in the queue.")
    return redirect("messaging:batch_detail", pk=pk)


class GatewayTestForm(TailwindFormMixin, forms.Form):
    phone = forms.CharField(max_length=25, label="Send a test to")
    body = forms.CharField(
        max_length=160,
        widget=forms.Textarea(attrs={"rows": 3}),
        initial="Test message from the school ERP.",
    )


@require_permission("core.change_school")
def gateway_test(request):
    """
    Send one real message now, so an administrator can tell whether the gateway works
    before a whole batch depends on it.
    """
    from .services import send_now, send_single

    form = GatewayTestForm(request.POST or None)
    result = None
    if request.method == "POST" and form.is_valid():
        try:
            message = send_single(
                request.school, form.cleaned_data["phone"], form.cleaned_data["body"], name="Gateway test"
            )
            ok = send_now(message)
            message.refresh_from_db()
            result = {"ok": ok, "message": message}
            audit(request, "sms.gateway_tested", message, message.status)
        except ValidationError as exc:
            form.add_error("phone", exc)
    return render(
        request,
        "messaging/gateway_test.html",
        {"form": form, "result": result, "page_title": "Test the SMS gateway"},
    )
