"""
The Bangla and English toggle: the school decides whether Bangla is offered, each person
chooses, and English is unchanged for everyone who keeps it.
"""

import re
from decimal import Decimal
from pathlib import Path

from django.core.management import call_command
from django.test import Client

from core.management.commands.compile_translations import parse_po
from examinations.services import publish_exam, save_mark

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "locale" / "bn" / "LC_MESSAGES" / "django.po"
TRANSLATED_TEMPLATES = [
    "templates/layouts/nav.html",
    "templates/layouts/base.html",
    "templates/portal/index.html",
    "templates/portal/attendance.html",
    "templates/portal/fees.html",
    "templates/portal/results.html",
    "templates/examinations/_official_results.html",
    "templates/examinations/report_card.html",
]


def login(user):
    client = Client()
    client.force_login(user)
    return client


def portal(client, erp):
    return client.get(f"/portal/results/?student={erp.student.pk}")


def test_english_is_unchanged_until_someone_chooses_bangla(erp):
    response = portal(login(erp.parent), erp)
    body = response.content.decode()
    assert response["Content-Language"] == "en" and '<html lang="en"' in body
    assert "My results" in body and "আমার ফলাফল" not in body
    # The toggle is offered because the school has Bangla switched on.
    assert 'value="bn"' in body and "বাংলা" in body


def test_the_toggle_switches_a_person_to_bangla_and_back(erp):
    client = login(erp.parent)
    response = client.post("/language/", {"language": "bn", "next": "/portal/results/"})
    assert response.status_code == 302 and response["Location"] == "/portal/results/"
    erp.parent.refresh_from_db()
    assert erp.parent.language == "bn"
    body = portal(client, erp).content.decode()
    assert '<html lang="bn"' in body and "আমার ফলাফল" in body and "Noto Sans Bengali" in body
    client.post("/language/", {"language": "en", "next": "/portal/results/"})
    assert "My results" in portal(client, erp).content.decode()
    # Another person's screens are unaffected by this choice.
    assert "Students" in login(erp.admin).get("/").content.decode()


def test_a_school_without_bangla_stays_in_english_whatever_was_chosen(erp):
    erp.parent.language = "bn"
    erp.parent.save()
    erp.school.bangla_enabled = False
    erp.school.save()
    client = login(erp.parent)
    body = portal(client, erp).content.decode()
    assert "My results" in body and 'value="bn"' not in body
    client.post("/language/", {"language": "bn", "next": "/"})
    erp.parent.refresh_from_db()
    assert erp.parent.language == "bn"  # kept for if the school switches Bangla on again
    assert "আমার ফলাফল" not in portal(client, erp).content.decode()


def test_the_schools_default_applies_until_a_person_chooses(erp):
    erp.school.default_language = "bn"
    erp.school.save()
    client = login(erp.parent)
    assert "আমার ফলাফল" in portal(client, erp).content.decode()
    client.post("/language/", {"language": "en", "next": "/"})
    assert "My results" in portal(client, erp).content.decode()


def test_the_toggle_only_returns_to_this_site(erp):
    response = login(erp.parent).post("/language/", {"language": "bn", "next": "https://evil.example/"})
    assert response["Location"] == "/"


def test_the_toggle_needs_a_signed_in_person_and_a_post(erp):
    assert Client().post("/language/", {"language": "bn"}).status_code == 302  # to sign in
    assert login(erp.parent).get("/language/").status_code == 405


def test_the_report_card_reads_in_bangla(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    erp.parent.language = "bn"
    erp.parent.save()
    body = login(erp.parent).get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").content.decode()
    assert "রিপোর্ট কার্ড" in body and "উত্তীর্ণ" in body and "শিক্ষাবর্ষ" in body


def test_signed_out_pages_stay_in_english(erp):
    assert "Sign in" in Client().get("/login/").content.decode()


# ------------------------------------------------------------------ the catalogue


def test_the_compiled_catalogue_matches_its_source():
    call_command("compile_translations", check=True)


def test_every_marked_string_on_the_family_screens_has_a_bangla_translation():
    catalogue = parse_po(CATALOGUE.read_text(encoding="utf-8"))
    missing = []
    for relative in TRANSLATED_TEMPLATES:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for msgid in re.findall(r'\{% translate "([^"]+)"', text):
            if not catalogue.get(msgid):
                missing.append(f"{relative}: {msgid}")
    assert not missing, missing
    assert all(value for key, value in catalogue.items() if key)
