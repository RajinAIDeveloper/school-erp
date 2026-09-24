"""
Phase 4b: a family applies online without an account, and follows the application on a
private page opened by a link only they hold.
"""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from admissions import services
from admissions.models import Application, ApplicationDocument
from core.models import AuditLog
from tests.admissions_support import apply_online, family, form_data, posted

ROOT = Path(__file__).resolve().parent.parent
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def send(client, school, row, **changes):
    return client.post(f"/apply/{school.slug}/{row.pk}/", posted(**changes))


# ------------------------------------------------------------------ the school's page


def test_the_landing_page_shows_the_open_round_its_classes_and_rules(erp, admissions):
    body = Client().get(f"/apply/{erp.school.slug}/").content.decode()
    assert "Admission 2027" in body and "Class 1" in body
    assert "born between 2 Jan 2020 and 1 Jan 2021" in body
    assert "Places left: 2" in body and "500" in body
    assert f"/apply/{erp.school.slug}/{admissions.row.pk}/" in body


def test_an_unpublished_round_is_not_shown_and_a_future_one_only_says_when(erp, admissions):
    admissions.round.is_published = False
    admissions.round.save()
    body = Client().get(f"/apply/{erp.school.slug}/").content.decode()
    assert "Admission 2027" not in body and "Applications are not open at the moment." in body
    admissions.round.is_published = True
    admissions.round.opens_on = admissions.today + timedelta(days=3)
    admissions.round.save()
    body = Client().get(f"/apply/{erp.school.slug}/").content.decode()
    assert "Applications open on" in body and f"/apply/{erp.school.slug}/{admissions.row.pk}/" not in body
    response = send(Client(), erp.school, admissions.row)
    assert response.status_code == 302 and response["Location"] == f"/apply/{erp.school.slug}/"
    assert not Application.objects.exists()


def test_every_public_page_is_private_to_the_visitor(erp, admissions):
    client = Client()
    application, raw = apply_online(admissions.row)
    for url in (
        f"/apply/{erp.school.slug}/",
        f"/apply/{erp.school.slug}/{admissions.row.pk}/",
        f"/apply/{erp.school.slug}/my/",
        f"/apply/{erp.school.slug}/t/{raw}/",
    ):
        response = client.get(url)
        assert response["Cache-Control"] == "no-store", url
        assert "noindex" in response["X-Robots-Tag"], url
        # Never sent to another site; "no-referrer" would make the browser send a form's
        # origin as "null", which the CSRF check refuses.
        assert response["Referrer-Policy"] == "same-origin", url


# ------------------------------------------------------------------ applying


def test_a_family_applies_and_gets_a_number_and_a_private_link(erp, admissions):
    client = Client()
    response = send(client, erp.school, admissions.row)
    assert response.status_code == 302 and response["Location"] == f"/apply/{erp.school.slug}/done/"
    application = Application.objects.get()
    assert application.reference == "APP-2027-0001"
    assert application.status == "submitted" and application.channel == "online"
    assert application.guardian_phone == "01711000001"
    assert application.consent_version == "2026-09" and application.consent_ip == "127.0.0.1"
    done = client.get(response["Location"]).content.decode()
    link = re.search(r"http://testserver(/apply/test/t/[^/<\s]+/)", done).group(1)
    raw = link.split("/")[-2]
    # Only the hash is kept: the raw token is nowhere in the database.
    assert application.token_hash == services.hash_token(raw) and raw not in application.token_hash
    assert not Application.objects.filter(token_hash=raw).exists()
    assert "APP-2027-0001" in done
    slip = client.get(f"/apply/{erp.school.slug}/my/{application.pk}/slip/")
    assert slip["Content-Type"] == "application/pdf" and slip.content.startswith(b"%PDF")
    assert slip["Cache-Control"] == "no-store"
    assert AuditLog.objects.filter(action="application.submitted", user=None).exists()


def test_a_real_browser_passes_the_csrf_check_and_keeps_its_page_after_the_session_key_changes(erp, admissions):
    client = Client(enforce_csrf_checks=True)
    client.get(f"/apply/{erp.school.slug}/{admissions.row.pk}/")
    token = client.cookies["csrftoken"].value
    origin = {"HTTP_ORIGIN": "http://testserver"}
    response = client.post(
        f"/apply/{erp.school.slug}/{admissions.row.pk}/", {**posted(), "csrfmiddlewaretoken": token}, **origin
    )
    assert response.status_code == 302, response.status_code
    application = Application.objects.get()
    page = f"/apply/{erp.school.slug}/my/"
    client.post(page, {"application": application.pk, "action": "withdraw", "csrfmiddlewaretoken": token}, **origin)
    application.refresh_from_db()
    assert application.status == "withdrawn"


def test_the_private_link_moves_into_the_session_and_leaves_the_address_bar(erp, admissions):
    application, raw = apply_online(admissions.row)
    stranger = Client()
    assert "Rafi" not in stranger.get(f"/apply/{erp.school.slug}/my/").content.decode()
    response = stranger.get(f"/apply/{erp.school.slug}/t/{raw}/")
    assert response.status_code == 302 and response["Location"] == f"/apply/{erp.school.slug}/my/"
    assert raw not in response["Location"]
    page = stranger.get(response["Location"]).content.decode()
    assert "Rafi Ahmed" in page and "APP-2027-0001" in page and "Submitted" in page
    assert "Birth registration certificate" in page  # still needed


def test_a_wrong_or_other_schools_link_opens_nothing(erp, admissions):
    application, raw = apply_online(admissions.row)
    assert Client().get(f"/apply/{erp.school.slug}/t/not-a-real-token/").status_code == 404
    erp.other.admissions_enabled = True
    erp.other.save()
    assert Client().get(f"/apply/{erp.other.slug}/t/{raw}/").status_code == 404


def test_the_honeypot_and_the_time_trap_stop_machines(erp, admissions):
    assert "could not be sent" in send(Client(), erp.school, admissions.row, hp_confirm="x").content.decode()
    too_fast = Client().post(f"/apply/{erp.school.slug}/{admissions.row.pk}/", posted(seconds_ago=1))
    assert "could not be sent" in too_fast.content.decode()
    unsigned = Client().post(f"/apply/{erp.school.slug}/{admissions.row.pk}/", {**form_data(), "started": "1"})
    assert "open too long" in unsigned.content.decode()
    assert not Application.objects.exists()


def test_too_many_sends_from_one_address_are_turned_away(erp, admissions):
    from admissions.public_views import TRIES

    client = Client()
    for _ in range(TRIES):
        client.post(f"/apply/{erp.school.slug}/{admissions.row.pk}/", posted(first_name=""))
    response = send(client, erp.school, admissions.row)
    assert "Too many applications" in response.content.decode()
    assert not Application.objects.exists()


def test_the_school_wide_ceiling_holds_whatever_the_address(erp, admissions, monkeypatch):
    from admissions import public_views

    monkeypatch.setattr(public_views, "SCHOOL_HOURLY", 2)
    for number, address in enumerate(("10.0.0.1", "10.0.0.2", "10.0.0.3")):
        Client(REMOTE_ADDR=address).post(
            f"/apply/{erp.school.slug}/{admissions.row.pk}/", posted(first_name=f"Child{number}")
        )
    assert Application.objects.count() == 2


@pytest.mark.parametrize(
    ("born", "accepted"),
    [("2020-01-01", False), ("2020-01-02", True), ("2021-01-01", True), ("2021-01-02", False)],
)
def test_the_date_of_birth_window_is_kept_to_the_day(erp, admissions, born, accepted):
    response = send(Client(), erp.school, admissions.row, date_of_birth=born)
    assert Application.objects.exists() is accepted
    if not accepted:
        assert "born between 2 Jan 2020 and 1 Jan 2021" in response.content.decode()


def test_the_form_checks_what_a_family_types(erp, admissions):
    body = send(
        Client(), erp.school, admissions.row, birth_registration_no="12345", guardian_phone="12", consent=None
    ).content.decode()
    assert "17 digits" in body and "Give a mobile number" in body
    assert not Application.objects.exists()
    send(
        Client(),
        erp.school,
        admissions.row,
        guardian_phone="+44 7700 900123",
        birth_registration_no="2020 2692 5000 00001",
    )
    application = Application.objects.get()
    assert application.guardian_phone == "+447700900123" and application.birth_registration_no == "20202692500000001"


def test_a_sibling_claim_needs_a_name_and_the_form_looks_nothing_up(erp, admissions):
    assert "name and class" in send(Client(), erp.school, admissions.row, sibling_claimed="on").content.decode()
    # Naming a child who is at the school, or one who is not, reads exactly the same.
    first = send(Client(), erp.school, admissions.row, sibling_claimed="on", sibling_details="Ayesha, Class 1 A")
    second = send(
        Client(),
        erp.school,
        admissions.row,
        sibling_claimed="on",
        sibling_details="Nobody, Class 9",
        first_name="Tanvir",
    )
    assert first.status_code == second.status_code == 302
    assert all(a.sibling is None for a in Application.objects.all())


def test_a_duplicate_is_flagged_for_staff_never_refused(erp, admissions):
    first, _ = apply_online(admissions.row, birth_registration_no="20202692500000001")
    second, _ = apply_online(admissions.row, first_name="Someone", birth_registration_no="20202692500000001")
    third, _ = apply_online(admissions.row)  # same name, date of birth and phone as the first
    assert first.possible_duplicate_of is None
    assert second.possible_duplicate_of == first and third.possible_duplicate_of == first


# ------------------------------------------------------------------ the family's page


def test_a_family_uploads_documents_checked_by_content(erp, admissions, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    application, raw = apply_online(admissions.row)
    client = family(erp.school, raw)
    url = f"/apply/{erp.school.slug}/my/"
    fake = SimpleUploadedFile("birth.pdf", b"MZ\x90\x00 not a pdf", content_type="application/pdf")
    client.post(url, {"application": application.pk, "action": "upload", "kind": "birth_certificate", "file": fake})
    assert not ApplicationDocument.objects.exists()
    pdf = SimpleUploadedFile("My Son's Birth Certificate.pdf", PDF, content_type="application/pdf")
    client.post(url, {"application": application.pk, "action": "upload", "kind": "birth_certificate", "file": pdf})
    document = ApplicationDocument.objects.get()
    assert document.uploaded_by is None and document.status == "waiting" and document.file_kind == "pdf"
    assert "Birth" not in document.file.name and document.file.name.startswith(f"admissions/{erp.school.pk}/")
    page = client.get(url).content.decode()
    assert "Waiting to be checked" in page and "Recent photo of the child" in page


def test_another_browser_cannot_act_on_the_application(erp, admissions):
    application, raw = apply_online(admissions.row)
    other, _ = apply_online(admissions.row, first_name="Tanvir")
    client = family(erp.school, raw)
    response = client.post(f"/apply/{erp.school.slug}/my/", {"application": other.pk, "action": "withdraw"})
    assert response.status_code == 404
    other.refresh_from_db()
    assert other.status == "submitted"
    assert client.get(f"/apply/{erp.school.slug}/my/{other.pk}/slip/").status_code == 404


def test_a_family_withdraws(erp, admissions):
    application, raw = apply_online(admissions.row)
    client = family(erp.school, raw)
    client.post(f"/apply/{erp.school.slug}/my/", {"application": application.pk, "action": "withdraw"})
    application.refresh_from_db()
    assert application.status == "withdrawn"
    event = application.events.get(to_status="withdrawn")
    assert event.by is None


def test_a_family_accepts_an_offer_but_not_once_it_has_lapsed(erp, admissions):
    application, raw = apply_online(admissions.row)
    services.transition(application, "under_review", user=erp.admin)
    services.transition(application, "offered", user=erp.admin, today=admissions.today)
    client = family(erp.school, raw)
    page = client.get(f"/apply/{erp.school.slug}/my/").content.decode()
    assert "Accept the place" in page and "offered a place" in page
    client.post(f"/apply/{erp.school.slug}/my/", {"application": application.pk, "action": "accept"})
    application.refresh_from_db()
    assert application.status == "accepted" and application.accepted_at is not None

    late, raw = apply_online(admissions.row, first_name="Tanvir")
    services.transition(late, "under_review", user=erp.admin)
    services.transition(late, "offered", user=erp.admin, today=admissions.today - timedelta(days=30))
    late.refresh_from_db()
    assert late.current_status == "lapsed"
    client = family(erp.school, raw)
    page = client.get(f"/apply/{erp.school.slug}/my/").content.decode()
    assert "Accept the place" not in page and "lapsed" in page
    response = client.post(f"/apply/{erp.school.slug}/my/", {"application": late.pk, "action": "accept"}, follow=True)
    assert "lapsed" in response.content.decode()
    late.refresh_from_db()
    assert late.status == "offered"  # the refused answer changed nothing


def test_a_new_link_from_the_office_closes_the_old_one_everywhere(erp, admissions):
    application, raw = apply_online(admissions.row)
    client = family(erp.school, raw)
    fresh = services.reissue_link(user=erp.staff, application=application)
    assert "Rafi" not in client.get(f"/apply/{erp.school.slug}/my/").content.decode()
    assert Client().get(f"/apply/{erp.school.slug}/t/{raw}/").status_code == 404
    assert "Rafi" in family(erp.school, fresh).get(f"/apply/{erp.school.slug}/my/").content.decode()


# ------------------------------------------------------------------ language


def test_a_visitor_switches_to_bangla_and_it_is_remembered(erp, admissions):
    client = Client()
    response = client.get(f"/apply/{erp.school.slug}/?lang=bn")
    assert response.status_code == 302 and response["Location"] == f"/apply/{erp.school.slug}/"
    body = client.get(f"/apply/{erp.school.slug}/").content.decode()
    assert '<html lang="bn"' in body and "ভর্তি" in body and "Noto Sans Bengali" in body
    form = client.get(f"/apply/{erp.school.slug}/{admissions.row.pk}/").content.decode()
    assert "Child's first name" not in form and "Send the application" not in form


def test_the_schools_default_applies_and_a_school_without_bangla_stays_english(erp, admissions):
    erp.school.default_language = "bn"
    erp.school.save()
    assert '<html lang="bn"' in Client().get(f"/apply/{erp.school.slug}/").content.decode()
    erp.school.bangla_enabled = False
    erp.school.save()
    client = Client()
    client.get(f"/apply/{erp.school.slug}/?lang=bn")
    body = client.get(f"/apply/{erp.school.slug}/").content.decode()
    assert '<html lang="en"' in body and "বাংলা" not in body


def test_every_family_facing_message_in_the_code_has_a_bangla_translation():
    from tests.test_language import CATALOGUE, parse_po

    catalogue = parse_po(CATALOGUE.read_text(encoding="utf-8"))
    missing = []
    for path in sorted((ROOT / "admissions").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for msgid in re.findall(r'(?:gettext|_)\(\s*"((?:[^"\\]|\\.)*)"\s*\)', text):
            if not catalogue.get(msgid):
                missing.append(f"{path.name}: {msgid}")
        for block in re.findall(r"_\(\s*((?:\"(?:[^\"\\]|\\.)*\"\s*)+)\)", text):
            msgid = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', block))
            if not catalogue.get(msgid):
                missing.append(f"{path.name}: {msgid}")
    assert not missing, missing


# ------------------------------------------------------------------ the module


def test_without_the_module_every_public_page_is_not_found(erp, admissions):
    application, raw = apply_online(admissions.row)
    erp.school.admissions_enabled = False
    erp.school.save()
    for url in (
        f"/apply/{erp.school.slug}/",
        f"/apply/{erp.school.slug}/{admissions.row.pk}/",
        f"/apply/{erp.school.slug}/my/",
        f"/apply/{erp.school.slug}/done/",
        f"/apply/{erp.school.slug}/t/{raw}/",
        f"/apply/{erp.school.slug}/my/{application.pk}/slip/",
    ):
        assert Client().get(url).status_code == 404, url


def test_an_inactive_school_has_no_admissions_pages(erp, admissions):
    erp.school.is_active = False
    erp.school.save()
    assert Client().get(f"/apply/{erp.school.slug}/").status_code == 404


def test_date_windows_from_ages():
    after, before = services.window_from_ages(date(2027, 1, 1), 6, 7)
    assert (after, before) == (date(2019, 1, 2), date(2021, 1, 1))
    # 29 February counts as 28 February in a year without one.
    assert services.years_before(date(2028, 2, 29), 6) == date(2022, 2, 28)
