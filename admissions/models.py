"""
Online admissions: a round of admission, the classes it admits into, and each application.

A round is one intake for one academic year: it opens and closes on set dates and admits into
one or more classes, each with its seats, its test or interview and its date-of-birth window.
A family applies without an account. They get an application number and a private link;
only a hash of the link's token is stored, so the database alone cannot open an application.

An application moves through its statuses only by `services.transition`, which checks each
move and writes it on the application's timeline and in the audit log.
"""

import uuid
from pathlib import PurePath

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from core.models import SchoolScopedModel


class AdmissionRound(SchoolScopedModel):
    class SiblingRule(models.TextChoices):
        NONE = "none", "Not considered"
        TIE_BREAK = "tie_break", "Breaks a tie in the merit list"
        PRIORITY = "priority", "Offered first among those who pass"

    academic_year = models.ForeignKey(
        "academics.AcademicYear",
        on_delete=models.PROTECT,
        related_name="admission_rounds",
        help_text="The year the children will join.",
    )
    name = models.CharField(max_length=120, help_text="e.g. Admission 2027")
    opens_on = models.DateField()
    closes_on = models.DateField()
    instructions = models.TextField(blank=True, help_text="Shown to families above the form.")
    instructions_bn = models.TextField("Instructions (Bangla)", blank=True)
    application_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, help_text="Paid at the school office. Zero for no fee."
    )
    fee_before_assessment = models.BooleanField(
        default=True, help_text="Families pay the fee before the test or interview."
    )
    offer_days = models.PositiveSmallIntegerField(
        default=7, help_text="Days a family has to accept an offered place before it lapses."
    )
    sibling_rule = models.CharField(max_length=10, choices=SiblingRule.choices, default=SiblingRule.TIE_BREAK)
    is_published = models.BooleanField(
        default=False, help_text="Families see the round only once it is published, and apply only while it is open."
    )

    class Meta:
        ordering = ["-opens_on", "-id"]
        constraints = [
            models.CheckConstraint(condition=Q(closes_on__gte=models.F("opens_on")), name="round_closes_after_it_opens")
        ]

    def __str__(self):
        return self.name

    def is_open(self, today=None):
        today = today or timezone.localdate()
        return self.is_published and self.opens_on <= today <= self.closes_on


RELATIONS = [
    ("mother", "Mother"),
    ("father", "Father"),
    ("grandparent", "Grandparent"),
    ("sibling", "Brother or sister"),
    ("uncle_aunt", "Uncle or aunt"),
    ("other", "Other"),
]

DOCUMENT_KINDS = [
    ("birth_certificate", _("Birth registration certificate")),
    ("photo", _("Recent photo of the child")),
    ("report_card", _("Last report card")),
    ("transfer_certificate", _("Transfer certificate")),
    ("guardian_nid", _("Guardian's national ID card")),
    ("other", _("Other document")),
]
DOCUMENT_LABELS = dict(DOCUMENT_KINDS)


class RoundClass(SchoolScopedModel):
    """One class a round admits into: its seats, how applicants are assessed, and who may apply."""

    class Assessment(models.TextChoices):
        NONE = "none", _("No test or interview")
        TEST = "test", _("Admission test")
        INTERVIEW = "interview", _("Interview")
        BOTH = "both", _("Admission test and interview")

    admission_round = models.ForeignKey(AdmissionRound, on_delete=models.CASCADE, related_name="classes")
    class_level = models.ForeignKey("academics.ClassLevel", on_delete=models.PROTECT, related_name="+")
    seats = models.PositiveSmallIntegerField()
    assessment = models.CharField(max_length=10, choices=Assessment.choices, default=Assessment.NONE)
    max_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    pass_score = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    born_on_or_after = models.DateField(null=True, blank=True)
    born_on_or_before = models.DateField(null=True, blank=True)
    required_documents = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["class_level__order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["admission_round", "class_level"], name="one_round_class_per_level"),
            models.CheckConstraint(
                condition=Q(born_on_or_after__isnull=True)
                | Q(born_on_or_before__isnull=True)
                | Q(born_on_or_before__gte=models.F("born_on_or_after")),
                name="round_class_birth_window_in_order",
            ),
        ]

    def __str__(self):
        return f"{self.class_level} · {self.admission_round}"

    def admits_birth_date(self, born):
        if self.born_on_or_after and born < self.born_on_or_after:
            return False
        return not (self.born_on_or_before and born > self.born_on_or_before)

    @property
    def document_labels(self):
        return [DOCUMENT_LABELS[kind] for kind in self.required_documents if kind in DOCUMENT_LABELS]


class Application(SchoolScopedModel):
    class Status(models.TextChoices):
        SUBMITTED = "submitted", _("Submitted")
        UNDER_REVIEW = "under_review", _("Under review")
        OFFERED = "offered", _("Offered a place")
        WAITLISTED = "waitlisted", _("On the waiting list")
        NOT_OFFERED = "not_offered", _("Not offered a place")
        LAPSED = "lapsed", _("Offer lapsed")
        ACCEPTED = "accepted", _("Place accepted")
        OFFER_DECLINED = "offer_declined", _("Place declined")
        WITHDRAWN = "withdrawn", _("Withdrawn")
        ENROLLED = "enrolled", _("Enrolled")

    class Channel(models.TextChoices):
        ONLINE = "online", "Online"
        OFFICE = "office", "At the office"

    class HeardFrom(models.TextChoices):
        SIBLING = "sibling", _("A brother or sister studies here")
        FAMILY = "family", _("Family or friends")
        WEBSITE = "website", _("The school's website")
        SOCIAL = "social", _("Social media")
        NEWSPAPER = "newspaper", _("A newspaper or poster")
        EVENT = "event", _("An open day or event")
        OTHER = "other", _("Somewhere else")

    round_class = models.ForeignKey(RoundClass, on_delete=models.PROTECT, related_name="applications")
    reference = models.CharField(max_length=20)
    token_hash = models.CharField(max_length=64, unique=True)
    token_generation = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SUBMITTED)
    channel = models.CharField(max_length=8, choices=Channel.choices, default=Channel.ONLINE)
    heard_from = models.CharField(max_length=10, choices=HeardFrom.choices, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)
    # The child
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    name_bn = models.CharField("Name (Bangla)", max_length=200, blank=True)
    gender = models.CharField(max_length=1, choices=[("M", "Male"), ("F", "Female"), ("O", "Other")])
    date_of_birth = models.DateField()
    religion = models.CharField(max_length=30, blank=True)
    birth_registration_no = models.CharField(max_length=17, blank=True)
    previous_school = models.CharField(max_length=200, blank=True)
    previous_class = models.CharField(max_length=50, blank=True)
    # The family
    guardian_name = models.CharField(max_length=200)
    guardian_relation = models.CharField(max_length=15, choices=RELATIONS)
    guardian_phone = models.CharField(max_length=25)
    guardian_email = models.EmailField(blank=True)
    guardian_occupation = models.CharField(max_length=100, blank=True)
    father_name = models.CharField("Father's name", max_length=150, blank=True)
    mother_name = models.CharField("Mother's name", max_length=150, blank=True)
    address = models.TextField(blank=True)
    # A brother or sister at the school: claimed by the family, confirmed only by staff.
    sibling_claimed = models.BooleanField(default=False)
    sibling_details = models.CharField(max_length=200, blank=True, help_text="The name and class the family gave.")
    sibling = models.ForeignKey(
        "students.Student", null=True, blank=True, on_delete=models.SET_NULL, related_name="sibling_applications"
    )
    sibling_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    sibling_verified_at = models.DateTimeField(null=True, blank=True)
    # The guardian's consent to the school holding the child's details (PDPA 2026 s.9).
    consent_version = models.CharField(max_length=20)
    consent_at = models.DateTimeField()
    consent_ip = models.GenericIPAddressField(null=True, blank=True)
    age_override_reason = models.CharField(max_length=200, blank=True)
    fee_waived_reason = models.CharField(max_length=200, blank=True)
    fee_waived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    fee_waived_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.CharField(max_length=300, blank=True)
    offer_expires_on = models.DateField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    merit_rank = models.PositiveIntegerField(null=True, blank=True)
    score = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    possible_duplicate_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="possible_duplicates"
    )
    student = models.OneToOneField(
        "students.Student", null=True, blank=True, on_delete=models.SET_NULL, related_name="application"
    )
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["school", "reference"], name="unique_application_reference")]
        permissions = [("decide_application", "Can offer, wait-list or turn down an application")]

    def __str__(self):
        return f"{self.reference} {self.child_name}"

    @property
    def child_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def offer_lapsed(self):
        """An offer past its expiry: lapsed, whether or not anyone has acted on it since."""
        return (
            self.status == self.Status.OFFERED
            and self.offer_expires_on is not None
            and self.offer_expires_on < timezone.localdate()
        )

    @property
    def current_status(self):
        return self.Status.LAPSED if self.offer_lapsed else self.status

    def get_current_status_display(self):
        return self.Status(self.current_status).label


def document_path(instance, filename):
    """Stored by school, under a random name: never the child's, never what the family called it."""
    return f"admissions/{instance.school_id}/{uuid.uuid4().hex}{PurePath(filename).suffix.lower()}"


class ApplicationDocument(SchoolScopedModel):
    class Status(models.TextChoices):
        WAITING = "waiting", _("Waiting to be checked")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Not accepted")

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=24, choices=DOCUMENT_KINDS)
    file = models.FileField(upload_to=document_path, max_length=200)
    file_kind = models.CharField(max_length=8)
    size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Empty when the family uploaded it.",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.WAITING)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    checked_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["kind", "id"]

    def __str__(self):
        return f"{self.get_kind_display()} ({self.application.reference})"


class ApplicationEvent(SchoolScopedModel):
    """The application's timeline, for staff: each change of status, check and note."""

    class Kind(models.TextChoices):
        STATUS = "status", "Status"
        NOTE = "note", "Note"
        DOCUMENT = "document", "Document"
        PAYMENT = "payment", "Payment"
        LINK = "link", "Private link"
        SIBLING = "sibling", "Sibling"
        DETAILS = "details", "Details"

    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16, blank=True)
    text = models.CharField(max_length=500, blank=True)
    by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Empty when the family did it.",
    )

    class Meta:
        ordering = ["-created_at", "-id"]


class ApplicationPayment(SchoolScopedModel):
    """An application fee paid at the school office, with its receipt and its entry in the ledger."""

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        BANK = "bank", "Bank transfer / cheque"
        MOBILE = "mobile", "Mobile banking (bKash / Nagad / Rocket)"

    METHOD_ACCOUNT_CODE = {"cash": "1010", "bank": "1020", "mobile": "1030"}

    application = models.ForeignKey(Application, on_delete=models.PROTECT, related_name="payments")
    receipt_no = models.CharField(max_length=20)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.CASH)
    reference = models.CharField(max_length=100, blank=True, help_text="Transaction ID / cheque no")
    date = models.DateField()
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.OneToOneField(
        "finance.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="application_payment"
    )
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    void_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "receipt_no"], name="unique_application_receipt_no"),
            models.CheckConstraint(condition=Q(amount__gt=0), name="application_payment_is_positive"),
        ]

    def __str__(self):
        return f"{self.receipt_no} {self.amount}"

    @property
    def is_voided(self):
        return self.voided_at is not None
