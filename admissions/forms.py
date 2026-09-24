"""
The application form, the same for a family online and for the office, and the office's setup forms.

The family's form is in their language, so every label here is marked for translation. It
asks nothing that would tell a stranger who studies at the school: a brother or sister is
named by the family and confirmed later by staff.
"""

import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from core.forms import SchoolModelForm, TailwindFormMixin

from .consent import CONSENT_TEXT
from .models import DOCUMENT_KINDS, AdmissionRound, Application, RoundClass

# Nothing is chosen for the family: a list that starts on "Girl" would send "Girl" for a boy
# whenever it was skipped.
GENDERS = [("", "—"), ("F", _("Girl")), ("M", _("Boy")), ("O", _("Other"))]
RELIGIONS = [
    ("", "—"),
    ("islam", _("Islam")),
    ("hinduism", _("Hinduism")),
    ("buddhism", _("Buddhism")),
    ("christianity", _("Christianity")),
    ("other", _("Other")),
]
RELATIONS = [
    ("", "—"),
    ("mother", _("Mother")),
    ("father", _("Father")),
    ("grandparent", _("Grandparent")),
    ("sibling", _("Brother or sister")),
    ("uncle_aunt", _("Uncle or aunt")),
    ("other", _("Other")),
]

DETAIL_FIELDS = [
    "first_name",
    "last_name",
    "name_bn",
    "gender",
    "date_of_birth",
    "religion",
    "birth_registration_no",
    "previous_school",
    "previous_class",
    "guardian_name",
    "guardian_relation",
    "guardian_phone",
    "guardian_email",
    "guardian_occupation",
    "father_name",
    "mother_name",
    "address",
    "sibling_claimed",
    "sibling_details",
    "heard_from",
]


def clean_phone(raw):
    """A Bangladeshi mobile, stored as 01XXXXXXXXX, or an overseas number written with its +."""
    from messaging.services import normalize_bd_phone
    from students.models import canonical_phone

    raw = (raw or "").strip()
    try:
        normalize_bd_phone(raw)
    except ValidationError:
        digits = re.sub(r"[^0-9]", "", raw)
        if not (raw.startswith("+") and 8 <= len(digits) <= 15):
            raise ValidationError(gettext("Give a mobile number, such as 01712345678.")) from None
        return "+" + digits
    return canonical_phone(raw)


class ApplicationForm(TailwindFormMixin, forms.Form):
    first_name = forms.CharField(label=_("Child's first name"), max_length=100)
    last_name = forms.CharField(label=_("Child's last name"), max_length=100, required=False)
    name_bn = forms.CharField(label=_("Child's name in Bangla"), max_length=200, required=False)
    gender = forms.ChoiceField(label=_("Girl or boy"), choices=GENDERS)
    date_of_birth = forms.DateField(label=_("Date of birth"), widget=forms.DateInput())
    religion = forms.ChoiceField(label=_("Religion"), choices=RELIGIONS, required=False)
    birth_registration_no = forms.CharField(
        label=_("Birth registration number"),
        max_length=25,
        required=False,
        help_text=_("The 17 digits on the online birth registration."),
    )
    previous_school = forms.CharField(label=_("School the child attends now"), max_length=200, required=False)
    previous_class = forms.CharField(label=_("Class there"), max_length=50, required=False)
    guardian_name = forms.CharField(label=_("Your name"), max_length=200)
    guardian_relation = forms.ChoiceField(label=_("You are the child's"), choices=RELATIONS)
    guardian_phone = forms.CharField(label=_("Your mobile number"), max_length=25)
    guardian_email = forms.EmailField(label=_("Your email (optional)"), required=False)
    guardian_occupation = forms.CharField(label=_("Your occupation"), max_length=100, required=False)
    father_name = forms.CharField(label=_("Father's name"), max_length=150, required=False)
    mother_name = forms.CharField(label=_("Mother's name"), max_length=150, required=False)
    address = forms.CharField(label=_("Home address"), widget=forms.Textarea(attrs={"rows": 2}), max_length=500)
    sibling_claimed = forms.BooleanField(label=_("A brother or sister studies at this school"), required=False)
    sibling_details = forms.CharField(
        label=_("Their name and class"),
        max_length=200,
        required=False,
        help_text=_("The school checks this; it can matter when places are given."),
    )
    heard_from = forms.ChoiceField(
        label=_("How did you hear about the school?"),
        choices=[("", "—"), *Application.HeardFrom.choices],
        required=False,
    )

    def clean_birth_registration_no(self):
        value = re.sub(r"\s", "", self.cleaned_data.get("birth_registration_no") or "")
        if value and not re.fullmatch(r"\d{17}", value):
            raise ValidationError(gettext("A birth registration number has 17 digits."))
        return value

    def clean_guardian_phone(self):
        return clean_phone(self.cleaned_data.get("guardian_phone"))

    def clean(self):
        data = super().clean()
        if data.get("sibling_claimed") and not (data.get("sibling_details") or "").strip():
            self.add_error("sibling_details", gettext("Give the brother's or sister's name and class."))
        if not data.get("sibling_claimed"):
            data["sibling_details"] = ""
        return data

    def details(self):
        return {name: self.cleaned_data.get(name) for name in DETAIL_FIELDS}


class PublicApplicationForm(ApplicationForm):
    consent = forms.BooleanField(label=CONSENT_TEXT)


class OfficeApplicationForm(ApplicationForm):
    consent = forms.BooleanField(
        label="The guardian has read and agreed to the consent on the form",
        help_text="The consent the online form asks for; its version is kept with the application.",
    )
    age_override_reason = forms.CharField(
        label="If the date of birth is outside the class's window: why the school accepts it",
        max_length=200,
        required=False,
    )

    def __init__(self, *args, office=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not office:
            # Correcting details later: consent was given when the application was made.
            del self.fields["consent"]
            del self.fields["age_override_reason"]


class RoundForm(SchoolModelForm):
    class Meta:
        model = AdmissionRound
        fields = [
            "name",
            "academic_year",
            "opens_on",
            "closes_on",
            "application_fee",
            "fee_before_assessment",
            "offer_days",
            "sibling_rule",
            "instructions",
            "instructions_bn",
            "is_published",
        ]
        widgets = {
            "opens_on": forms.DateInput(),
            "closes_on": forms.DateInput(),
            "instructions": forms.Textarea(attrs={"rows": 3}),
            "instructions_bn": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[
            "sibling_rule"
        ].help_text = "How a verified brother or sister at the school counts when places are given."

    def clean(self):
        data = super().clean()
        opens, closes = data.get("opens_on"), data.get("closes_on")
        if opens and closes and closes < opens:
            self.add_error("closes_on", "A round closes on or after the day it opens.")
        if data.get("application_fee") is not None and data["application_fee"] < 0:
            self.add_error("application_fee", "The fee cannot be below zero.")
        if data.get("offer_days") is not None and not 1 <= data["offer_days"] <= 60:
            self.add_error("offer_days", "Give families 1 to 60 days to accept.")
        return data


class RoundClassForm(SchoolModelForm):
    required_documents = forms.MultipleChoiceField(
        choices=DOCUMENT_KINDS[:-1], widget=forms.CheckboxSelectMultiple, required=False
    )
    youngest = forms.IntegerField(
        label="Youngest age", min_value=0, max_value=25, required=False, help_text="Or give ages instead of dates."
    )
    oldest = forms.IntegerField(label="Oldest age", min_value=0, max_value=25, required=False)
    ages_on = forms.DateField(
        label="Ages on", required=False, widget=forms.DateInput(), help_text="Usually 1 January of the year they join."
    )

    class Meta:
        model = RoundClass
        fields = [
            "class_level",
            "seats",
            "assessment",
            "max_score",
            "pass_score",
            "born_on_or_after",
            "born_on_or_before",
            "required_documents",
        ]
        widgets = {"born_on_or_after": forms.DateInput(), "born_on_or_before": forms.DateInput()}

    def __init__(self, *args, admission_round=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.admission_round = admission_round
        if admission_round is not None:
            self.instance.admission_round = admission_round
        self.fields["class_level"].queryset = self.fields["class_level"].queryset.filter(is_active=True)
        self.fields["born_on_or_after"].label = "Born on or after"
        self.fields["born_on_or_before"].label = "Born on or before"
        self.fields["max_score"].help_text = "The most a test or interview can score."
        self.fields["pass_score"].help_text = "Below this, a child is not offered a place."

    def clean(self):
        from .services import window_from_ages

        data = super().clean()
        youngest, oldest, on = data.get("youngest"), data.get("oldest"), data.get("ages_on")
        if any(value is not None for value in (youngest, oldest, on)):
            if None in (youngest, oldest, on):
                raise ValidationError(
                    "To fill the dates from ages, give the youngest age, the oldest age and the date."
                )
            try:
                data["born_on_or_after"], data["born_on_or_before"] = window_from_ages(on, youngest, oldest)
            except ValidationError as error:
                self.add_error("oldest", error)
        after, before = data.get("born_on_or_after"), data.get("born_on_or_before")
        if after and before and before < after:
            self.add_error("born_on_or_before", "This date comes before the other one.")
        level = data.get("class_level")
        taken = RoundClass.objects.filter(admission_round=self.admission_round, class_level=level)
        if level is not None and taken.exclude(pk=self.instance.pk).exists():
            self.add_error("class_level", "This round already admits into that class.")
        seats = data.get("seats")
        if seats is not None and seats < 1:
            self.add_error("seats", "A class admits at least one child.")
        top, bar = data.get("max_score"), data.get("pass_score")
        if top is not None and bar is not None and bar > top:
            self.add_error("pass_score", "The pass mark cannot be above the most a child can score.")
        if data.get("assessment") == RoundClass.Assessment.NONE:
            data["max_score"] = data["pass_score"] = None
        return data
