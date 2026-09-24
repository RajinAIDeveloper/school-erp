"""
Hardening from the audit: an SMS template cannot plant script on the compose page, and no
posted return address can send someone off this site.
"""

import pytest
from django.test import Client

from core.security import safe_next

EVIL = "https://evil.example/login"


def login(user):
    client = Client()
    client.force_login(user)
    return client


# ------------------------------------------------------------------ SMS compose


def test_a_template_body_cannot_become_script_on_the_compose_page(erp):
    from messaging.models import SMSTemplate

    SMSTemplate.objects.create(school=erp.school, name="Trap", body="</script><script>alert('x')</script>")
    body = login(erp.admin).get("/sms/compose/").content.decode()
    assert "<script>alert(" not in body
    assert 'id="sms-template-bodies"' in body
    assert "\\u003C/script\\u003E" in body  # escaped inside the JSON, still the same text once parsed


# ------------------------------------------------------------------ return addresses


@pytest.mark.parametrize(
    "address",
    [EVIL, "//evil.example/", "https:/\\evil.example", "javascript:alert(1)", ""],
)
def test_an_address_off_this_site_falls_back_to_the_views_own(rf, address):
    request = rf.post("/")
    assert safe_next(request, address, "/fallback/") == "/fallback/"


def test_an_address_on_this_site_is_kept(rf):
    request = rf.post("/")
    assert safe_next(request, "/portal/fees/?student=3", "/fallback/") == "/portal/fees/?student=3"
    assert safe_next(request, "http://testserver/x/", "/fallback/") == "http://testserver/x/"


def test_a_failed_online_payment_returns_only_to_this_site(erp, invoice):
    # Online payment is off, so starting one fails and the family is sent back.
    client = login(erp.parent)
    response = client.post(f"/fees/{invoice.pk}/pay-online/", {"amount": "100", "back": EVIL})
    assert response.status_code == 302 and response["Location"] == "/portal/fees/"
    response = client.post(f"/fees/{invoice.pk}/pay-online/", {"amount": "100", "back": "/portal/fees/?student=1"})
    assert response["Location"] == "/portal/fees/?student=1"


def test_retrying_an_sms_returns_only_to_this_site(erp):
    from messaging.models import SMSMessage

    message = SMSMessage.objects.create(
        school=erp.school, phone="01712345679", body="Hello", status=SMSMessage.Status.FAILED
    )
    response = login(erp.admin).post(f"/sms/{message.pk}/retry/", {"next": EVIL})
    assert response.status_code == 302 and response["Location"] == "/sms/outbox/"


def test_checking_in_returns_only_to_this_site(erp):
    erp.school.staff_self_checkin = True
    erp.school.save()
    response = login(erp.teacher).post("/attendance/staff/check-in/", {"next": "//evil.example/"})
    assert response.status_code == 302 and response["Location"] == "/attendance/staff/"


def test_a_refused_login_request_returns_only_to_this_site(erp):
    # The guardian already has a login, so a second is refused and the admin is sent back.
    response = login(erp.admin).post(f"/users/provision/guardian/{erp.guardian.pk}/", {"next": EVIL})
    assert response.status_code == 302 and response["Location"] == "/users/"


# ------------------------------------------------------------------ the payment gateway's address


def test_a_payer_is_sent_only_to_sslcommerz(erp, invoice, monkeypatch):
    import fees.online as online
    from fees.models import OnlinePayment

    erp.school.payment_gateway = "sslcommerz"
    erp.school.sslcommerz_store_id, erp.school.sslcommerz_store_password = "store", "secret"
    erp.school.save()
    monkeypatch.setattr(
        online, "http_post", lambda url, data: {"status": "SUCCESS", "GatewayPageURL": "https://evil.example/pay"}
    )
    response = login(erp.parent).post(f"/fees/{invoice.pk}/pay-online/", {"amount": "100"})
    assert response["Location"] == "/portal/fees/"
    assert OnlinePayment.objects.get().status == OnlinePayment.Status.FAILED


# ------------------------------------------------------------------ the visitor's real address


@pytest.mark.parametrize(
    ("hops", "forwarded", "expected"),
    [
        (0, "203.0.113.9", "10.0.0.2"),  # no proxy trusted: the header is ignored
        (1, "203.0.113.9", "203.0.113.9"),  # nginx in front
        (1, "6.6.6.6, 203.0.113.9", "203.0.113.9"),  # the visitor's own entry is not believed
        (2, "203.0.113.9, 198.51.100.7", "203.0.113.9"),  # Cloudflare, then nginx
        (2, "203.0.113.9", "10.0.0.2"),  # fewer entries than proxies: something is wrong
        (1, "not-an-address", "10.0.0.2"),
        (1, "", "10.0.0.2"),
    ],
)
def test_the_client_address_is_read_through_trusted_proxies_only(rf, settings, hops, forwarded, expected):
    from core.security import client_ip

    settings.TRUSTED_PROXY_COUNT = hops
    request = rf.get("/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR=forwarded)
    assert client_ip(request) == expected


@pytest.fixture
def lookup_on(erp):
    from django.core.cache import cache

    from examinations.public import ensure_codes

    cache.clear()
    erp.school.public_results_enabled = True
    erp.school.save()
    ensure_codes([erp.student])
    erp.student.refresh_from_db()
    yield erp
    cache.clear()


def ask(erp, visitor, student_id=None, code="WRNG-CODE"):
    return Client(REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR=visitor).post(
        f"/results/{erp.school.slug}/", {"student_id": student_id or erp.student.student_id, "code": code}
    )


def test_behind_a_proxy_one_visitor_cannot_lock_the_lookup_for_everyone(lookup_on, settings):
    from examinations.public import ATTEMPTS

    settings.TRUSTED_PROXY_COUNT = 1
    for _ in range(ATTEMPTS):
        ask(lookup_on, "203.0.113.9", student_id="NOBODY")
    assert "Too many attempts" in ask(lookup_on, "203.0.113.9", student_id="NOBODY").content.decode()
    # Another family, arriving through the same proxy, still gets in.
    body = ask(lookup_on, "198.51.100.7", code=lookup_on.student.result_code).content.decode()
    assert "Too many attempts" not in body and "Ayesha" in body


def test_guessing_one_students_code_from_many_addresses_is_stopped(lookup_on, settings):
    from examinations.public import ATTEMPTS

    settings.TRUSTED_PROXY_COUNT = 1
    for n in range(ATTEMPTS):
        ask(lookup_on, f"203.0.113.{n + 1}")
    body = ask(lookup_on, "198.51.100.7", code=lookup_on.student.result_code).content.decode()
    assert "Too many attempts" in body and "Ayesha" not in body
    # Other students are unaffected.
    assert "Too many attempts" not in ask(lookup_on, "198.51.100.8", student_id="S2").content.decode()


def test_the_audit_log_records_the_visitors_address(erp, rf, settings):
    from core.models import AuditLog, audit

    settings.TRUSTED_PROXY_COUNT = 1
    request = rf.post("/", REMOTE_ADDR="10.0.0.2", HTTP_X_FORWARDED_FOR="203.0.113.9")
    request.school, request.user = erp.school, erp.admin
    audit(request, "test.address")
    assert AuditLog.objects.get(action="test.address").ip_address == "203.0.113.9"


# ------------------------------------------------------------------ the menu


def test_the_results_menu_leads_to_the_everyday_result_screens(erp):
    from django.urls import reverse

    body = login(erp.admin).get("/").content.decode()
    for label, name in (
        ("Enter marks", "examinations:marks"),
        ("Result sheets", "examinations:results"),
        ("Combined results", "examinations:combined_list"),
        ("Exam series", "examinations:series_list"),
    ):
        assert f'href="{reverse(name)}"' in body and label in body, label
    teacher = login(erp.teacher).get("/").content.decode()
    assert "Enter marks" in teacher and "Result sheets" in teacher


def test_basic_settings_is_shown_to_those_who_can_change_it(erp):
    assert "Basic Settings" in login(erp.admin).get("/").content.decode()
    assert "Basic Settings" in login(erp.accountant).get("/").content.decode()  # fee heads, accounts
    assert "Basic Settings" not in login(erp.teacher).get("/").content.decode()
    assert "Basic Settings" not in login(erp.parent).get("/portal/").content.decode()
