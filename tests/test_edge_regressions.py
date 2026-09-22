from datetime import date, time, timedelta
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import transaction
from django.test import Client
from django.utils import timezone

from downloads.models import DownloadCategory, DownloadItem
from examinations.models import Mark
from fees.models import FeeInvoice
from finance.models import Account, JournalEntry, record_simple_entry
from holidays.models import Holiday, holiday_dates_between, is_holiday
from messaging.models import SMSMessage
from messaging.services import queue_batch


def test_holidays_exclude_weekend_and_open_event(erp):
    monday = date(2026, 9, 21)
    friday = date(2026, 9, 25)
    assert not is_holiday(erp.school, monday)
    assert is_holiday(erp.school, friday)
    Holiday.objects.create(
        school=erp.school,
        name="Sports day",
        holiday_type="event",
        closes_school=False,
        start_date=monday,
        end_date=monday,
    )
    assert not is_holiday(erp.school, monday)
    Holiday.objects.create(school=erp.school, name="Closed", start_date=monday, end_date=monday + timedelta(days=1))
    assert is_holiday(erp.school, monday)
    assert holiday_dates_between(erp.school, monday, monday + timedelta(days=1)) == {monday, monday + timedelta(days=1)}


def test_public_download_and_tenant_denial(erp, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cat = DownloadCategory.objects.create(school=erp.school, name="Public")
    item = DownloadItem.objects.create(
        school=erp.school,
        category=cat,
        title="Prospectus",
        audience="public",
        file=SimpleUploadedFile("prospectus.pdf", b"%PDF-sample"),
    )
    c = Client()
    r = c.get(f"/downloads/{item.pk}/file/")
    assert r.status_code == 200
    assert b"".join(r.streaming_content) == b"%PDF-sample"
    c.force_login(erp.admin)
    assert c.get(f"/downloads/{item.pk}/file/").status_code == 200
    from users.models import User

    foreign = User.objects.create_user("foreign-user", school=erp.other, password="Test-pass-9842")
    c.force_login(foreign)
    assert c.get(f"/downloads/{item.pk}/file/").status_code == 404


def test_download_audience_restricts_unlinked_guardian(erp, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cat = DownloadCategory.objects.create(school=erp.school, name="Private")
    other = type(erp.level).objects.create(school=erp.school, name="Class 2", order=2)
    item = DownloadItem.objects.create(
        school=erp.school,
        category=cat,
        title="Class 2 file",
        audience="students",
        file=SimpleUploadedFile("class2.txt", b"restricted"),
    )
    item.class_levels.add(other)
    c = Client()
    c.force_login(erp.parent)
    assert c.get(f"/downloads/{item.pk}/file/").status_code == 403
    item.class_levels.set([erp.level])
    assert c.get(f"/downloads/{item.pk}/file/").status_code == 200


def test_sms_worker_delivers_once_and_logs_console(erp, capsys):
    batch = queue_batch(erp.school, "Notice", "custom", "Hello", [("Parent", "01712345679")], erp.admin)
    call_command("process_sms", limit=100)
    assert batch.messages.get().status == "sent"
    call_command("process_sms", limit=100)
    assert batch.messages.count() == 1
    output = capsys.readouterr().out
    assert output.count("[SMS ->") == 1


def test_sms_http_gateway_failure_is_recorded_without_real_network(erp, settings):
    from messaging.backends import HttpSMSBackend

    erp.school.sms_api_url = "https://sms.example.com/send"
    erp.school.sms_api_key = "SECRET"
    erp.school.save()
    batch = queue_batch(erp.school, "Notice", "custom", "Hello", [("Parent", "01712345679")], erp.admin)
    msg = batch.messages.get()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"response_code":"403"}'

    with patch("messaging.backends.urlopen", return_value=FakeResponse()) as mock:
        assert HttpSMSBackend().deliver(msg) is False
    msg.refresh_from_db()
    assert msg.status == "failed"
    assert "rejected" in msg.provider_response.lower()
    assert "api_key=SECRET" in mock.call_args.args[0].full_url


def test_manual_journal_reversal_preserves_old_period(erp):
    cash = Account.objects.get(school=erp.school, code="1010")
    income = Account.objects.get(school=erp.school, code="4900")
    posted = record_simple_entry(erp.school, date(2026, 8, 15), "Donation", cash, income, 100, source="manual")
    assert cash.balance(date(2026, 8, 1), date(2026, 8, 31)) == 100
    rev = posted.reverse(erp.admin, "Correction")
    assert rev.status == "posted"
    assert rev.date == timezone.localdate()
    assert cash.balance() == 0
    if timezone.localdate().month != 8:
        assert cash.balance(date(2026, 8, 1), date(2026, 8, 31)) == 100
    with pytest.raises(ValidationError):
        posted.post()


def test_payroll_negative_net_rejected_on_form(erp):
    from finance.views import payroll_create

    c = Client()
    c.force_login(erp.admin)
    r = c.post(
        "/finance/payroll/new/",
        {"employee": erp.employee.pk, "month": "2026-09-01", "basic": "100", "allowance": "0", "deduction": "200"},
    )
    assert r.status_code == 200
    assert b"net pay" in r.content.lower()
    from finance.models import Payroll

    assert not Payroll.objects.exists()


def test_account_cycle_rejected_on_form(erp):
    from django.forms import modelform_factory

    from core.forms import SchoolModelForm

    parent = Account.objects.get(school=erp.school, code="1010")
    child = Account.objects.get(school=erp.school, code="1020")
    child.parent = parent
    child.save()
    Form = modelform_factory(Account, form=SchoolModelForm, fields=["code", "name", "account_type", "parent"])
    form = Form(
        {"code": parent.code, "name": parent.name, "account_type": parent.account_type, "parent": child.pk},
        instance=parent,
        school=erp.school,
    )
    assert not form.is_valid() and "parent" in form.errors


def test_subject_analysis_grading_bounds(erp):
    from examinations.models import GradeRule

    rule = GradeRule(scale=erp.scale, letter="X", min_percent=70, max_percent=20, grade_point=4)
    with pytest.raises(ValidationError):
        rule.full_clean()
