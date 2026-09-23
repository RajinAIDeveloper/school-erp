"""Composing, per-recipient rendering, the outbox, retries and the worker."""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from core.models import AuditLog
from messaging.backends import BaseSMSBackend
from messaging.models import SMSBatch, SMSMessage, SMSTemplate
from messaging.notifications import ensure_default_templates
from messaging.services import describe_length, queue_batch, render_body, segments
from students.models import Enrollment, Guardian, Student, StudentGuardian


def second_family(erp, name="Rumi", phone="01712345690", roll=2):
    student = Student.objects.create(
        school=erp.school,
        student_id=f"SMS{roll}",
        first_name=name,
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school,
        student=student,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=roll,
    )
    guardian = Guardian.objects.create(school=erp.school, full_name=f"{name}'s parent", phone=phone)
    StudentGuardian.objects.create(student=student, guardian=guardian, relation="father", is_primary=True)
    return student, guardian


def test_segment_counting_handles_gsm_and_bangla():
    assert segments("") == 0
    assert segments("Hello") == 1
    assert segments("a" * 160) == 1
    assert segments("a" * 161) == 2
    # Bangla is Unicode, so a much shorter message already costs two parts.
    bangla = "আপনার সন্তান আজ অনুপস্থিত ছিল" * 3
    assert len(bangla) > 70
    assert segments(bangla) >= 2
    assert "Unicode" in describe_length(bangla)
    assert "GSM" in describe_length("Hello")


def test_render_body_fills_placeholders_and_blanks_the_missing():
    body = "Dear guardian, {student} of {class} owes {amount}."
    rendered = render_body(body, {"student": "Ayesha", "class": "Class 1 - A", "amount": None})
    assert rendered == "Dear guardian, Ayesha of Class 1 - A owes ."


def test_compose_renders_one_message_per_child_with_their_own_name(erp):
    second_family(erp)
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/sms/compose/",
        {
            "title": "Exam notice",
            "recipients": "class",
            "section": erp.section.pk,
            "body": "Dear guardian, {student} of {class} sits an exam on Sunday. - {school}",
        },
    )
    assert response.status_code == 302
    bodies = set(SMSMessage.objects.values_list("body", flat=True))
    assert any("Ayesha" in body for body in bodies)
    assert any("Rumi" in body for body in bodies)
    assert all("{student}" not in body for body in bodies)
    assert AuditLog.objects.filter(action="sms.queued").exists()


def test_compose_preview_shows_the_count_and_a_sample_without_queueing(erp):
    second_family(erp)
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/sms/compose/",
        {
            "title": "Notice",
            "recipients": "class",
            "section": erp.section.pk,
            "body": "Hello {student}",
            "preview": "1",
        },
    )
    assert response.status_code == 200
    assert b"2 guardian(s)" in response.content
    assert not SMSMessage.objects.exists()


def test_compose_skips_families_who_opted_out(erp):
    _student, guardian = second_family(erp)
    guardian.sms_opt_in = False
    guardian.save()
    client = Client()
    client.force_login(erp.admin)
    client.post(
        "/sms/compose/",
        {"title": "Notice", "recipients": "class", "section": erp.section.pk, "body": "Hello {student}"},
    )
    assert SMSMessage.objects.count() == 1
    assert "Ayesha" in SMSMessage.objects.get().body


def test_compose_requires_a_class_for_a_class_batch(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post("/sms/compose/", {"title": "x", "recipients": "class", "body": "Hi"})
    assert response.status_code == 200
    assert not SMSMessage.objects.exists()


def test_batch_list_and_detail_show_delivery_counts(erp):
    batch = queue_batch(
        erp.school, "Test", "custom", "Hello", [("One", "01712345678"), ("Two", "01712345679")], erp.admin
    )
    batch.messages.update(status="sent")
    failed = batch.messages.first()
    failed.status = "failed"
    failed.provider_response = "Gateway refused"
    failed.save()

    client = Client()
    client.force_login(erp.admin)
    listing = client.get("/sms/").content
    assert b"Test" in listing

    detail = client.get(f"/sms/batch/{batch.pk}/").content
    assert b"Gateway refused" in detail
    assert b"Retry 1 failed" in detail


def test_retrying_a_batch_requeues_only_the_failures(erp):
    batch = queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    message = batch.messages.get()
    message.status = "failed"
    message.attempts = 3
    message.save()

    client = Client()
    client.force_login(erp.admin)
    client.post(f"/sms/batch/{batch.pk}/retry/")
    message.refresh_from_db()
    assert message.status == "queued" and message.attempts == 0


def test_outbox_filters_by_status_and_exports(erp):
    queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    client = Client()
    client.force_login(erp.admin)
    assert b"01712345678" in client.get("/sms/outbox/?status=queued").content
    assert b"01712345678" not in client.get("/sms/outbox/?status=sent").content
    assert "Attempts" in client.get("/sms/outbox/?format=csv").content.decode("utf-8-sig")


def test_worker_sends_queued_messages_once(erp, capsys):
    from django.core.management import call_command

    queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    call_command("process_sms", verbosity=0)
    message = SMSMessage.objects.get()
    assert message.status == "sent" and message.attempts == 1

    call_command("process_sms", verbosity=0)
    message.refresh_from_db()
    assert message.attempts == 1


def test_worker_gives_up_after_three_attempts(erp, settings):
    """Each retry is scheduled further out, so a refusing gateway is not hammered."""
    from django.core.management import call_command

    settings.SMS_BACKEND = "tests.test_messaging.AlwaysFailingBackend"
    queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    for _ in range(4):
        call_command("process_sms", verbosity=0)
        # Pretend the backoff has elapsed, rather than making the test sleep for it.
        SMSMessage.objects.all().update(next_attempt_at=None)
    message = SMSMessage.objects.get()
    assert message.attempts == 3
    assert message.status == "failed"
    assert "Gateway down" in message.provider_response


def test_a_failed_message_waits_before_it_is_tried_again(erp, settings):
    from django.core.management import call_command
    from django.utils import timezone

    settings.SMS_BACKEND = "tests.test_messaging.AlwaysFailingBackend"
    queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    call_command("process_sms", verbosity=0)
    message = SMSMessage.objects.get()
    assert message.status == "queued" and message.attempts == 1
    assert message.next_attempt_at > timezone.now()

    # A second pass straight away does nothing: the message is not due yet.
    call_command("process_sms", verbosity=0)
    assert SMSMessage.objects.get().attempts == 1


def test_a_message_stranded_by_a_crashed_worker_is_reclaimed(erp):
    """A worker killed mid-send used to leave its message as 'processing' for ever."""
    from datetime import timedelta

    from django.core.management import call_command
    from django.utils import timezone

    queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    stranded = SMSMessage.objects.get()
    SMSMessage.objects.filter(pk=stranded.pk).update(
        status="processing", attempts=1, claimed_at=timezone.now() - timedelta(minutes=30)
    )
    call_command("process_sms", verbosity=0)
    stranded.refresh_from_db()
    assert stranded.status == "sent"
    # The attempt made before the crash still counts, so a message that kills the worker
    # every time runs out of attempts instead of looping.
    assert stranded.attempts == 2


def test_a_claim_that_is_still_fresh_is_left_alone(erp):
    from django.core.management import call_command
    from django.utils import timezone

    queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    busy = SMSMessage.objects.get()
    SMSMessage.objects.filter(pk=busy.pk).update(status="processing", claimed_at=timezone.now())
    call_command("process_sms", verbosity=0)
    busy.refresh_from_db()
    assert busy.status == "processing" and busy.attempts == 0


def test_two_workers_racing_for_one_message_send_it_once(erp, capsys):
    """The claim is a conditional update, so only one worker can win it."""
    from django.core.management import call_command

    queue_batch(erp.school, "Test", "custom", "Hello", [("One", "01712345678")], erp.admin)
    call_command("process_sms", verbosity=0)
    call_command("process_sms", verbosity=0)
    message = SMSMessage.objects.get()
    assert message.status == "sent" and message.attempts == 1
    assert capsys.readouterr().out.count("[SMS ->") == 1


def test_a_credential_message_keeps_its_audit_trail_but_not_the_password(erp):
    """The password has to reach the queue to be sent; it does not stay there afterwards."""
    from django.core.management import call_command

    from students.models import Guardian
    from users.services import provision_login

    erp.school.notify_admission_sms = True
    erp.school.save()
    guardian = Guardian.objects.create(school=erp.school, full_name="Texted", phone="01712345686")
    _account, password = provision_login(school=erp.school, user=erp.admin, profile=guardian, send_sms=True)
    queued = SMSMessage.objects.get()
    assert password in queued.body and queued.redact_after_send

    call_command("process_sms", verbosity=0)
    queued.refresh_from_db()
    assert queued.status == "sent"
    assert password not in queued.body
    # What a school still needs for an audit survives: who, which number, and when.
    assert queued.recipient_name and queued.phone == "8801712345686" and queued.sent_at


def test_a_credential_message_keeps_its_text_while_retries_remain(erp, settings):
    """Redacting a message that has not gone yet would destroy the password unsent."""
    from django.core.management import call_command

    from students.models import Guardian
    from users.services import provision_login

    settings.SMS_BACKEND = "tests.test_messaging.AlwaysFailingBackend"
    erp.school.notify_admission_sms = True
    erp.school.save()
    guardian = Guardian.objects.create(school=erp.school, full_name="Texted", phone="01712345686")
    _account, password = provision_login(school=erp.school, user=erp.admin, profile=guardian, send_sms=True)
    call_command("process_sms", verbosity=0)
    message = SMSMessage.objects.get()
    assert message.status == "queued" and password in message.body

    for _ in range(3):
        SMSMessage.objects.all().update(next_attempt_at=None)
        call_command("process_sms", verbosity=0)
    message.refresh_from_db()
    assert message.status == "failed" and password not in message.body


class AlwaysFailingBackend(BaseSMSBackend):
    """Used by the retry test to stand in for a gateway that is refusing everything."""

    def send(self, message):
        raise RuntimeError("Gateway down")


def test_gateway_test_sends_one_message_immediately(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post("/sms/gateway-test/", {"phone": "01712345678", "body": "Checking the gateway."})
    assert response.status_code == 200
    assert b"accepted the message" in response.content
    message = SMSMessage.objects.get()
    assert message.status == "sent" and message.recipient_name == "Gateway test"
    assert AuditLog.objects.filter(action="sms.gateway_tested").exists()


def test_gateway_test_rejects_a_bad_number(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post("/sms/gateway-test/", {"phone": "123", "body": "Hi"})
    assert response.status_code == 200
    assert not SMSMessage.objects.exists()


def test_gateway_test_is_administrator_only(erp):
    client = Client()
    client.force_login(erp.accountant)
    assert client.get("/sms/gateway-test/").status_code == 403


def test_notification_settings_seed_editable_templates(erp):
    client = Client()
    client.force_login(erp.admin)
    response = client.post(
        "/settings/notifications/",
        {"notify_absence_sms": "on", "notify_payment_sms": "on"},
    )
    assert response.status_code == 302
    erp.school.refresh_from_db()
    assert erp.school.notify_absence_sms and erp.school.notify_payment_sms
    assert not erp.school.notify_results_sms
    assert SMSTemplate.objects.filter(school=erp.school, name="Absence alert").exists()


def test_default_templates_are_created_once(erp):
    assert len(ensure_default_templates(erp.school)) == 5
    assert ensure_default_templates(erp.school) == []


def test_notification_settings_are_administrator_only(erp):
    client = Client()
    client.force_login(erp.teacher)
    assert client.get("/settings/notifications/").status_code == 403


def test_queue_batch_still_rejects_an_invalid_number(erp):
    with pytest.raises(ValidationError):
        queue_batch(erp.school, "Test", "custom", "Hello", [("Bad", "123")], erp.admin)
    assert not SMSMessage.objects.exists()
    assert not SMSBatch.objects.exists()


def test_queue_batch_deduplicates_the_same_number(erp):
    batch = queue_batch(
        erp.school,
        "Test",
        "custom",
        "Hello",
        [("One", "01712345678"), ("Same", "+8801712345678")],
        erp.admin,
    )
    assert batch.total == 1
