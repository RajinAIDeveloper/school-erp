"""Helpers shared by the admissions tests: a family's form, and an application made through it."""

import time

from django.core import signing
from django.test import Client

from admissions import services
from admissions.forms import PublicApplicationForm
from admissions.public_views import FORM_SALT


def form_data(**changes):
    data = {
        "first_name": "Rafi",
        "last_name": "Ahmed",
        "gender": "M",
        "date_of_birth": "2020-06-15",
        "guardian_name": "Nasrin Ahmed",
        "guardian_relation": "mother",
        "guardian_phone": "01711000001",
        "address": "House 1, Road 2, Dhaka",
        "consent": "on",
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


def posted(seconds_ago=60, **changes):
    """What the browser sends: the form, a start time long enough ago, and the empty trap field."""
    return {**form_data(**changes), "started": signing.dumps(time.time() - seconds_ago, salt=FORM_SALT)}


def apply_online(row, **changes):
    """An application made through the family's form. Returns (application, raw token)."""
    form = PublicApplicationForm(form_data(**changes))
    assert form.is_valid(), form.errors
    return services.submit(round_class=row, details=form.details())


def family(school, raw):
    """A browser that has opened the family's private link."""
    client = Client()
    response = client.get(f"/apply/{school.slug}/t/{raw}/")
    assert response.status_code == 302, response.status_code
    return client


def login(user):
    client = Client()
    client.force_login(user)
    return client
