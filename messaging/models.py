from django.conf import settings
from django.db import models

from core.models import SchoolScopedModel


class SMSTemplate(SchoolScopedModel):
    """
    Placeholders available: {school}, {name}, {student}, {class}, {roll}, {amount}, {date}, {invoice}
    """

    name = models.CharField(max_length=100)
    body = models.TextField(max_length=480)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["school", "name"], name="unique_sms_template_per_school")]

    def __str__(self):
        return self.name

    def render(self, **ctx):
        text = self.body
        for k, v in ctx.items():
            text = text.replace("{" + k + "}", str(v))
        return text


class SMSBatch(SchoolScopedModel):
    """One 'send' action; groups the individual messages for reporting."""

    class Recipients(models.TextChoices):
        CLASS = "class", "A class / section (guardians)"
        ALL_GUARDIANS = "all_guardians", "All guardians"
        ALL_STAFF = "all_staff", "All teachers & staff"
        TEACHERS = "teachers", "Teachers only"
        CUSTOM = "custom", "Custom numbers"

    title = models.CharField(max_length=150)
    recipients = models.CharField(max_length=15, choices=Recipients.choices)
    body = models.TextField(max_length=480)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "SMS batches"

    def __str__(self):
        return self.title

    @property
    def total(self):
        return self.messages.count()

    @property
    def sent_count(self):
        return self.messages.filter(status=SMSMessage.Status.SENT).count()

    @property
    def failed_count(self):
        return self.messages.filter(status=SMSMessage.Status.FAILED).count()

    @property
    def queued_count(self):
        return self.messages.filter(status__in=[SMSMessage.Status.QUEUED, SMSMessage.Status.PROCESSING]).count()


class SMSMessage(SchoolScopedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Processing"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    batch = models.ForeignKey(SMSBatch, null=True, blank=True, on_delete=models.CASCADE, related_name="messages")
    recipient_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=25)
    body = models.TextField(max_length=480)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    attempts = models.PositiveSmallIntegerField(default=0)
    dedupe_key = models.CharField(
        max_length=120,
        blank=True,
        help_text="Identifies an automatic message so the same event never queues twice.",
    )
    provider_response = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["school", "dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="unique_automatic_message_per_event",
            )
        ]

    def __str__(self):
        return f"{self.phone} [{self.status}]"
