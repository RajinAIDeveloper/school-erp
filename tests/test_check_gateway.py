"""
check_gateway: the school's SSLCommerz store login tried before families pay, and payments whose
confirmation went missing recovered from the gateway's own record. SSLCommerz is stubbed.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

import fees.online as online
from fees.models import FeePayment, OnlinePayment


@pytest.fixture
def store(erp):
    erp.school.payment_gateway = "sslcommerz"
    erp.school.sslcommerz_store_id, erp.school.sslcommerz_store_password = "teststore", "secret"
    erp.school.save()
    return erp.school


def gateway(monkeypatch, *, login="DONE", holds=None):
    """SSLCommerz as a stub: the transaction query answers `login`, and `holds` maps tran_id to a val_id."""
    asked = []

    def fake_get(url, params):
        asked.append((url, params))
        if "merchantTransIDvalidationAPI" in url:
            element = []
            if holds and params["tran_id"] in holds:
                element = [{"status": "VALID", "val_id": holds[params["tran_id"]], "tran_id": params["tran_id"]}]
            return {"APIConnect": login, "no_of_trans_found": len(element), "element": element}
        attempt = OnlinePayment.objects.get(tran_id=next(t for t, v in holds.items() if v == params["val_id"]))
        return {
            "status": "VALID",
            "tran_id": attempt.tran_id,
            "amount": str(attempt.amount),
            "currency": "BDT",
            "val_id": params["val_id"],
            "bank_tran_id": "BANK9",
        }

    monkeypatch.setattr(online, "http_get", fake_get)
    return asked


def run(**options):
    out = StringIO()
    call_command("check_gateway", stdout=out, **options)
    return out.getvalue()


def test_an_accepted_store_login_is_reported_without_charging_anything(store, monkeypatch):
    asked = gateway(monkeypatch)
    printed = run()
    assert "sandbox (test money)" in printed and "store login accepted" in printed
    url, params = asked[0]
    assert url.startswith("https://sandbox.sslcommerz.com/validator/api/merchantTransIDvalidationAPI.php")
    assert params["tran_id"].startswith("CHECK-") and params["store_id"] == "teststore"
    assert not OnlinePayment.objects.exists()


@pytest.mark.parametrize(
    ("login", "message"), [("FAILED", "refused"), ("INACTIVE", "inactive"), ("INVALID_REQUEST", "invalid")]
)
def test_a_refused_store_login_fails_the_check(store, monkeypatch, login, message):
    gateway(monkeypatch, login=login)
    out = StringIO()
    with pytest.raises(CommandError, match="not ready"):
        call_command("check_gateway", stdout=out)
    assert message in out.getvalue()


def test_an_unreachable_gateway_or_missing_details_fail_the_check(store, monkeypatch):
    def down(url, params):
        raise OSError("timed out")

    monkeypatch.setattr(online, "http_get", down)
    with pytest.raises(CommandError):
        run()
    store.sslcommerz_store_password = ""
    store.save()
    with pytest.raises(CommandError):
        run()


def open_attempt(erp, invoice, tran_id, minutes_ago=60):
    attempt = OnlinePayment.objects.create(
        school=erp.school, invoice=invoice, tran_id=tran_id, gateway="sslcommerz", amount=invoice.balance
    )
    OnlinePayment.objects.filter(pk=attempt.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
    return attempt


def test_settle_records_a_payment_whose_confirmation_went_missing(erp, store, invoice, monkeypatch):
    lost = open_attempt(erp, invoice, "S1-LOST")
    abandoned = open_attempt(erp, invoice, "S1-ABANDONED")
    recent = open_attempt(erp, invoice, "S1-RECENT", minutes_ago=5)
    gateway(monkeypatch, holds={"S1-LOST": "VAL-LOST"})

    printed = run()
    assert "3 payment(s)" not in printed and "2 payment(s) still open" in printed
    assert "--settle" in printed and not FeePayment.objects.exists()

    printed = run(settle=True)
    lost.refresh_from_db()
    assert lost.status == OnlinePayment.Status.PAID and lost.payment.amount == lost.amount
    assert f"recorded as {lost.payment.receipt_no}" in printed
    assert "S1-ABANDONED: the gateway holds no payment" in printed
    abandoned.refresh_from_db()
    recent.refresh_from_db()
    assert abandoned.status == recent.status == OnlinePayment.Status.STARTED
    # Running it again changes nothing: the receipt is written once.
    run(settle=True)
    assert FeePayment.objects.count() == 1


def test_a_demonstration_school_is_reported_and_not_sent_to_sslcommerz(erp, settings, monkeypatch):
    settings.ALLOW_DEMO_PAYMENTS = True
    erp.school.payment_gateway = "demo"
    erp.school.save()
    asked = gateway(monkeypatch)
    assert "Demonstration gateway, allowed on this server" in run()
    assert asked == []
