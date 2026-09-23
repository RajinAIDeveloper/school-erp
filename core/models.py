from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class HttpsURLField(models.URLField):
    """
    A URL field whose forms assume https when no scheme is typed.

    Django 6 changes that default from http to https and warns until a choice is made.
    Making it here rather than setting the transitional flag means the answer travels with
    the field, and a school typing "bulksmsbd.net" into the gateway box gets the secure
    address, not the one that would send its API key in clear.
    """

    def formfield(self, **kwargs):
        return super().formfield(**{"assume_scheme": "https", **kwargs})


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class AssessmentSystem(models.TextChoices):
    """
    Which rulebook a class's results follow.

    The default is the school's own rules: a percentage and a grade from the school's scale for
    every paper, which is how the system has always worked. Each programme a school teaches is
    then chosen explicitly, per class or per exam, so one school can run a national-curriculum
    stream and a Cambridge stream side by side without either's rules touching the other.
    """

    OWN = "own", "School's own rules (percentage and grade)"
    NATIONAL = "national", "Bangladesh national curriculum (SSC board rules)"
    CAMBRIDGE = "cambridge", "Cambridge (IGCSE, O Level, AS and A Level)"
    EDEXCEL = "edexcel", "Pearson Edexcel (International GCSE, International AS and A Level)"
    IB_MYP = "ib_myp", "IB Middle Years Programme (criteria to grades 1-7)"


LANGUAGES = [("en", "English"), ("bn", "বাংলা")]


class School(TimeStampedModel):
    """
    The tenant. One row today; many rows when the platform becomes SaaS.
    Also holds the "Basic Settings" (identity, currency, SMS gateway).
    """

    name = models.CharField(max_length=200)
    short_name = models.CharField(max_length=50, blank=True)
    slug = models.SlugField(unique=True, help_text="Used for the subdomain later, e.g. dhaka-model")
    eiin = models.CharField("EIIN", max_length=10, blank=True, help_text="Bangladesh institution ID")
    motto = models.CharField(max_length=200, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=25, blank=True)
    email = models.EmailField(blank=True)
    website = HttpsURLField(blank=True)
    logo = models.ImageField(upload_to="school/logos/", blank=True)
    principal_name = models.CharField(max_length=150, blank=True)
    currency = models.CharField(max_length=3, default=settings.ERP_DEFAULT_CURRENCY)
    currency_symbol = models.CharField(max_length=5, default=settings.ERP_DEFAULT_CURRENCY_SYMBOL)
    country = models.CharField(max_length=2, default=settings.ERP_DEFAULT_COUNTRY)
    timezone = models.CharField(max_length=50, default="Asia/Dhaka")
    # Weekend days as comma separated ISO weekday numbers (1=Mon ... 7=Sun). BD default: Fri, Sat
    weekend_days = models.CharField(max_length=20, default="5,6")
    books_locked_until = models.DateField(
        null=True,
        blank=True,
        help_text="Nothing may be posted on or before this date. Set it once a month has been reported on.",
    )
    late_fee_per_day = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        default=0,
        help_text="Charged for each day an invoice is overdue. Zero disables late fees.",
    )
    late_fee_cap = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        help_text="Most that may be charged on one invoice. Zero means no cap.",
    )
    sibling_discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=(
            "Taken off every younger sibling's fees when two or more children share a primary "
            "guardian. The eldest pays in full. Zero switches it off. 0 to 100."
        ),
    )
    # Notifications. Every one of these is off until a school turns it on, because each
    # message costs the school money and reaches a family's phone.
    notify_absence_sms = models.BooleanField("Tell guardians about an absence", default=False)
    notify_payment_sms = models.BooleanField("Confirm fee payments by SMS", default=False)
    notify_due_sms = models.BooleanField("Allow fee reminder SMS", default=False)
    notify_results_sms = models.BooleanField("Announce published results", default=False)
    notify_admission_sms = models.BooleanField("Welcome newly admitted students", default=False)
    staff_self_checkin = models.BooleanField(
        default=False, help_text="Let teachers and staff record their own arrival and departure."
    )
    assessment_system = models.CharField(
        max_length=10,
        choices=AssessmentSystem.choices,
        default=AssessmentSystem.OWN,
        help_text=(
            "The rules results are calculated by. A class can override this, for a school that runs "
            "both a national-curriculum and an English-medium stream."
        ),
    )
    # Language. A school can offer Bangla or keep the interface in English; each person then
    # switches between them with the toggle in the header.
    bangla_enabled = models.BooleanField(
        "Offer the Bangla interface", default=True, help_text="Lets each person switch the screens to Bangla."
    )
    default_language = models.CharField(
        max_length=5,
        choices=LANGUAGES,
        default="en",
        help_text="What people see until they choose for themselves.",
    )
    public_results_enabled = models.BooleanField(
        "Publish results online",
        default=False,
        help_text=(
            "Families can look up a published result with the student ID and date of birth, without signing in."
        ),
    )
    # Online fees. Money settles straight into the school's own merchant account; the software
    # never holds it, which the Payment and Settlement Systems Act 2024 requires.
    payment_gateway = models.CharField(
        max_length=12,
        choices=[
            ("none", "Off"),
            ("sslcommerz", "SSLCommerz (bKash, Nagad, Rocket, cards, banks)"),
            ("demo", "Demonstration (no money moves)"),
        ],
        default="none",
    )
    sslcommerz_store_id = models.CharField("SSLCommerz store ID", max_length=100, blank=True)
    sslcommerz_store_password = models.CharField("SSLCommerz store password", max_length=200, blank=True)
    sslcommerz_sandbox = models.BooleanField(
        "Use the SSLCommerz sandbox", default=True, help_text="Clear this only with live credentials."
    )
    # SMS gateway (generic HTTP)
    sms_sender_id = models.CharField(max_length=20, blank=True)
    sms_api_url = HttpsURLField(blank=True, help_text="Gateway endpoint, e.g. https://bulksmsbd.net/api/smsapi")
    sms_api_key = models.CharField(max_length=200, blank=True)
    sms_extra_params = models.CharField(
        max_length=300, blank=True, help_text="Extra query params as key=value&key2=value2"
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            # A rate above 100 would make a fee line negative, and a "paid" invoice nobody
            # paid. The form checks it too; this is the floor under every other path.
            models.CheckConstraint(
                condition=models.Q(sibling_discount_percent__gte=0, sibling_discount_percent__lte=100),
                name="sibling_discount_is_a_percentage",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def weekend_day_numbers(self):
        return {int(d) for d in self.weekend_days.split(",") if d.strip().isdigit()}


class SchoolQuerySet(models.QuerySet):
    def for_school(self, school):
        return self.filter(school=school)


class SchoolScopedModel(TimeStampedModel):
    """
    Every business record inherits this so tenant isolation is enforced at the model level.
    """

    school = models.ForeignKey(School, on_delete=models.PROTECT, related_name="+")

    objects = SchoolQuerySet.as_manager()

    class Meta:
        abstract = True

    def clean(self):
        from django.core.exceptions import ValidationError

        super().clean()
        if not self.school_id:
            return
        errors = {}
        for field in self._meta.fields:
            if field.many_to_one or field.one_to_one:
                if field.name == "school" or not getattr(self, field.attname, None):
                    continue
                related = getattr(self, field.name)
                if hasattr(related, "school_id") and related.school_id != self.school_id:
                    errors[field.name] = "Select a record from the same school."
        if errors:
            raise ValidationError(errors)


class AuditLog(models.Model):
    """Records sensitive actions: payments, result publication, attendance edits, role changes."""

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="audit_logs", null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=100)
    model = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=50, blank=True)
    description = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} by {self.user} at {self.created_at:%Y-%m-%d %H:%M}"


def audit(request, action, obj=None, description=""):
    """Helper: audit(request, "payment.created", payment, "Receipt 0001")"""
    AuditLog.objects.create(
        school=getattr(request, "school", None),
        user=request.user if request.user.is_authenticated else None,
        action=action,
        model=obj._meta.label if obj is not None else "",
        object_id=str(obj.pk) if obj is not None else "",
        description=description,
        ip_address=request.META.get("REMOTE_ADDR"),
    )
