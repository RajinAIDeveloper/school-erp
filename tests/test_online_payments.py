"""
Paying fees online: a payment counts only when the gateway confirms it, server to server, for
the amount asked; the receipt is written once however many times the gateway calls back; and
the demonstration gateway is refused where it is not allowed.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

import fees.online as online_module
from fees.models import FeePayment, OnlinePayment
from finance.models import Account


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def demo(erp, settings, invoice):
    settings.ALLOW_DEMO_PAYMENTS = True
    erp.school.payment_gateway = "demo"
    erp.school.save()
    return invoice


@pytest.fixture
def ssl(erp, invoice, monkeypatch):
    erp.school.payment_gateway = "sslcommerz"
    erp.school.sslcommerz_store_id = "teststore"
    erp.school.sslcommerz_store_password = "secret"
    erp.school.save()
    sent = {}

    def fake_post(url, data):
        sent.update(url=url, data=data)
        return {"status": "SUCCESS", "GatewayPageURL": "https://sandbox.sslcommerz.com/pay/abc", "sessionkey": "k"}

    monkeypatch.setattr(online_module, "http_post", fake_post)
    return sent


def confirm(monkeypatch, **overrides):
    def fake_get(url, params):
        attempt = OnlinePayment.objects.get()
        reply = {
            "status": "VALID",
            "tran_id": attempt.tran_id,
            "amount": str(attempt.amount),
            "currency": "BDT",
            "val_id": params["val_id"],
            "bank_tran_id": "BANK123",
            "card_type": "BKASH-BKash",
        }
        reply.update(overrides)
        return reply

    monkeypatch.setattr(online_module, "http_get", fake_get)


def clearing(erp):
    return Account.objects.get(school=erp.school, code="1040").balance()


# ============================================================ the demonstration gateway


def test_a_family_pays_through_the_demonstration_gateway_once(erp, demo):
    family = login(erp.parent)
    response = family.post(f"/fees/{demo.pk}/pay-online/", {"amount": "1000"})
    attempt = OnlinePayment.objects.get()
    assert response.status_code == 302 and response["Location"].endswith(f"/fees/online/{attempt.tran_id}/demo/")
    assert b"no money moves" in Client().get(response["Location"]).content
    page = Client().post(response["Location"], {"outcome": "pay"})
    assert b"Payment received" in page.content
    Client().post(response["Location"], {"outcome": "pay"})  # a replay
    assert FeePayment.objects.count() == 1
    payment = FeePayment.objects.get()
    assert payment.method == "online" and payment.received_by is None
    demo.refresh_from_db()
    assert demo.status == "paid"
    assert clearing(erp) == Decimal("1000.00")


def test_the_demonstration_gateway_is_refused_where_it_is_not_allowed(erp, demo, settings):
    settings.ALLOW_DEMO_PAYMENTS = False
    response = login(erp.parent).post(f"/fees/{demo.pk}/pay-online/", {"amount": "1000"})
    assert response.status_code == 302 and not OnlinePayment.objects.exists()
    attempt = OnlinePayment.objects.create(
        school=erp.school, invoice=demo, tran_id="S-DEMO", gateway="demo", amount=Decimal(100)
    )
    assert Client().get(f"/fees/online/{attempt.tran_id}/demo/").status_code == 403
    assert Client().post(f"/fees/online/{attempt.tran_id}/demo/", {"outcome": "pay"}).status_code == 403
    assert not FeePayment.objects.exists()


def test_a_family_cannot_pay_another_familys_invoice(erp, demo):
    from django.contrib.auth.models import Group

    from users.models import User

    stranger = User.objects.create_user(username="stranger", school=erp.school, password="Test-pass-9842")
    stranger.groups.add(Group.objects.get(name="Guardian"))
    assert login(stranger).post(f"/fees/{demo.pk}/pay-online/", {"amount": "1000"}).status_code == 403


def test_the_amount_must_fit_the_balance(erp, demo):
    login(erp.parent).post(f"/fees/{demo.pk}/pay-online/", {"amount": "1500"})
    assert not OnlinePayment.objects.exists()


# ============================================================ SSLCommerz


def test_only_a_gateway_confirmation_records_the_payment_once(erp, invoice, ssl, monkeypatch):
    response = login(erp.parent).post(f"/fees/{invoice.pk}/pay-online/", {"amount": "400"})
    assert response["Location"] == "https://sandbox.sslcommerz.com/pay/abc"
    attempt = OnlinePayment.objects.get()
    data = ssl["data"]
    assert ssl["url"].startswith("https://sandbox.sslcommerz.com/gwprocess/v4/api.php")
    assert (data["total_amount"], data["currency"], data["tran_id"]) == ("400.00", "BDT", attempt.tran_id)
    assert data["ipn_url"].endswith("/fees/online/notify/")
    # A success page with no confirmation writes nothing.
    Client().post(f"/fees/online/{attempt.tran_id}/success/", {"status": "VALID"})
    assert not FeePayment.objects.exists()
    confirm(monkeypatch)
    page = Client().post(f"/fees/online/{attempt.tran_id}/success/", {"val_id": "V1", "status": "VALID"})
    assert b"Payment received" in page.content
    # The gateway's notification arrives as well, and changes nothing.
    assert Client().post("/fees/online/notify/", {"tran_id": attempt.tran_id, "val_id": "V1"}).status_code == 200
    assert FeePayment.objects.count() == 1
    attempt.refresh_from_db()
    assert (attempt.status, attempt.bank_tran_id) == ("paid", "BANK123")
    assert FeePayment.objects.get().reference == "BANK123"
    invoice.refresh_from_db()
    assert invoice.status == "partial"


def test_a_confirmation_for_a_different_amount_is_held_for_review(erp, invoice, ssl, monkeypatch):
    login(erp.parent).post(f"/fees/{invoice.pk}/pay-online/", {"amount": "400"})
    attempt = OnlinePayment.objects.get()
    confirm(monkeypatch, amount="4.00")
    Client().post(f"/fees/online/{attempt.tran_id}/success/", {"val_id": "V1"})
    attempt.refresh_from_db()
    assert attempt.status == "review" and "4.00" in attempt.note
    assert not FeePayment.objects.exists()


def test_another_currency_or_transaction_is_held_for_review(erp, invoice, ssl, monkeypatch):
    login(erp.parent).post(f"/fees/{invoice.pk}/pay-online/", {"amount": "400"})
    attempt = OnlinePayment.objects.get()
    confirm(monkeypatch, currency="USD")
    online_module.notification(attempt.tran_id, "V1")
    attempt.refresh_from_db()
    assert attempt.status == "review" and not FeePayment.objects.exists()


def test_a_failed_confirmation_records_nothing(erp, invoice, ssl, monkeypatch):
    login(erp.parent).post(f"/fees/{invoice.pk}/pay-online/", {"amount": "400"})
    attempt = OnlinePayment.objects.get()
    confirm(monkeypatch, status="INVALID_TRANSACTION")
    page = Client().post(f"/fees/online/{attempt.tran_id}/success/", {"val_id": "V1"})
    assert b"not completed" in page.content
    attempt.refresh_from_db()
    assert attempt.status == "failed" and not FeePayment.objects.exists()


def test_money_taken_after_the_invoice_was_paid_at_the_office_is_held_for_review(erp, invoice, ssl, monkeypatch):
    from datetime import date

    from fees.services import collect_payment

    login(erp.parent).post(f"/fees/{invoice.pk}/pay-online/", {"amount": "1000"})
    collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=1000,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )
    confirm(monkeypatch)
    attempt = OnlinePayment.objects.get()
    online_module.notification(attempt.tran_id, "V1")
    attempt.refresh_from_db()
    assert attempt.status == "review" and "Refund" in attempt.note
    assert FeePayment.objects.count() == 1


def test_a_cancelled_return_does_not_undo_a_confirmed_payment(erp, invoice, ssl, monkeypatch):
    login(erp.parent).post(f"/fees/{invoice.pk}/pay-online/", {"amount": "400"})
    attempt = OnlinePayment.objects.get()
    confirm(monkeypatch)
    online_module.notification(attempt.tran_id, "V1")
    Client().post(f"/fees/online/{attempt.tran_id}/cancel/")
    attempt.refresh_from_db()
    assert attempt.status == "paid"


def test_a_gateway_that_will_not_start_says_so(erp, invoice, ssl, monkeypatch):
    monkeypatch.setattr(
        online_module, "http_post", lambda url, data: {"status": "FAILED", "failedreason": "Store inactive"}
    )
    with pytest.raises(ValidationError) as caught:
        online_module.start_online_payment(
            user=erp.parent,
            invoice=invoice,
            amount="400",
            urls={
                key: (lambda t: f"https://school.example/{t}") for key in ("success", "fail", "cancel", "ipn", "demo")
            },
        )
    assert "Store inactive" in " ".join(caught.value.messages)


def test_the_payment_settings_never_show_the_password(erp):
    erp.school.sslcommerz_store_password = "keep-me"
    erp.school.save()
    client = login(erp.admin)
    page = client.get("/settings/payments/").content.decode()
    assert "keep-me" not in page
    client.post(
        "/settings/payments/",
        {
            "payment_gateway": "sslcommerz",
            "sslcommerz_store_id": "store1",
            "sslcommerz_store_password": "",
            "sslcommerz_sandbox": "on",
        },
    )
    erp.school.refresh_from_db()
    assert (erp.school.payment_gateway, erp.school.sslcommerz_store_password) == ("sslcommerz", "keep-me")


def test_the_portal_offers_to_pay_online_only_when_switched_on(erp, invoice):
    family = login(erp.parent)
    assert "Pay online" not in family.get(f"/portal/fees/?student={erp.student.pk}").content.decode()
    erp.school.payment_gateway = "sslcommerz"
    erp.school.save()
    assert "Pay online" in family.get(f"/portal/fees/?student={erp.student.pk}").content.decode()
