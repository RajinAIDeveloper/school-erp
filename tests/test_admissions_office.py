"""
Phase 4b: the admission office. Statuses move only by the allowed moves, offering is the
managers' decision, and the application fee goes through the ledger with its own receipt.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from admissions import services
from admissions.models import Application, ApplicationDocument, ApplicationPayment
from finance.models import JournalEntry
from finance.services import reverse_journal
from tests.admissions_support import apply_online, form_data, login
from users.models import User

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def principal(erp):
    user = User.objects.create_user(username="principal", school=erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name="Principal"))
    return user


# ------------------------------------------------------------------ statuses


def test_only_the_allowed_moves_are_made(erp, admissions):
    application, _ = apply_online(admissions.row)
    with pytest.raises(ValidationError, match="cannot be moved"):
        services.transition(application, "accepted", user=erp.admin)
    services.transition(application, "under_review", user=erp.staff)
    services.transition(application, "withdrawn", user=erp.staff, reason="Moved to Chittagong")
    with pytest.raises(ValidationError):
        services.transition(application, "under_review", user=erp.admin)
    application.refresh_from_db()
    assert application.status == "withdrawn"
    assert [e.to_status for e in application.events.order_by("pk")] == ["submitted", "under_review", "withdrawn"]


def test_offering_and_turning_down_are_for_the_managers(erp, admissions):
    application, _ = apply_online(admissions.row)
    services.transition(application, "under_review", user=erp.staff)
    for move in ("offered", "waitlisted", "not_offered"):
        with pytest.raises(PermissionDenied):
            services.transition(application, move, user=erp.staff, reason="x")
    head = principal(erp)
    with pytest.raises(ValidationError, match="reason"):
        services.transition(application, "not_offered", user=head)
    services.transition(application, "not_offered", user=head, reason="Did not sit the test")
    application.refresh_from_db()
    assert application.status == "not_offered" and application.decision_reason == "Did not sit the test"


def test_a_family_may_only_withdraw_accept_or_decline(erp, admissions):
    application, _ = apply_online(admissions.row)
    with pytest.raises(PermissionDenied):
        services.transition(application, "under_review")


def test_offers_never_exceed_the_seats(erp, admissions):
    offered = []
    for name in ("Rafi", "Tanvir", "Mitu"):
        application, _ = apply_online(admissions.row, first_name=name)
        services.transition(application, "under_review", user=erp.admin)
        offered.append(application)
    services.transition(offered[0], "offered", user=erp.admin)
    services.transition(offered[1], "offered", user=erp.admin)
    with pytest.raises(ValidationError, match="seat"):
        services.transition(offered[2], "offered", user=erp.admin)
    # A lapsed offer frees its seat; making the offer again is allowed.
    Application.objects.filter(pk=offered[1].pk).update(offer_expires_on=date.today() - timedelta(days=1))
    services.transition(offered[2], "offered", user=erp.admin)
    services.transition(offered[1], "lapsed", user=erp.admin)
    offered[1].refresh_from_db()
    assert offered[1].status == "lapsed"


def test_the_expiry_of_an_offer_follows_the_rounds_days(erp, admissions):
    application, _ = apply_online(admissions.row)
    services.transition(application, "under_review", user=erp.admin)
    services.transition(application, "offered", user=erp.admin, today=date(2026, 10, 1))
    application.refresh_from_db()
    assert application.offer_expires_on == date(2026, 10, 8)


# ------------------------------------------------------------------ the office


def test_a_walk_in_application_can_accept_a_date_of_birth_outside_the_window_with_a_reason(erp, admissions):
    client = login(erp.staff)
    url = f"/admissions/classes/{admissions.row.pk}/apply/"
    data = form_data(date_of_birth="2019-03-01")
    response = client.post(url, data)
    assert response.status_code == 200 and not Application.objects.exists()
    assert "born between" in response.content.decode()
    response = client.post(url, {**data, "age_override_reason": "Repeating Class 1 on the head's approval"})
    application = Application.objects.get()
    assert response["Location"] == f"/admissions/applications/{application.pk}/"
    assert application.channel == "office" and application.age_override_reason.startswith("Repeating")
    # The family's slip is printed once, from the session that made the link.
    slip = client.get(f"/admissions/applications/{application.pk}/slip/")
    assert slip["Content-Type"] == "application/pdf" and slip["Cache-Control"] == "no-store"
    other = login(erp.admin).get(f"/admissions/applications/{application.pk}/slip/")
    assert other.status_code == 302


def test_a_round_closed_to_families_still_takes_walk_ins(erp, admissions):
    admissions.round.closes_on = admissions.today - timedelta(days=1)
    admissions.round.opens_on = admissions.today - timedelta(days=5)
    admissions.round.save()
    with pytest.raises(ValidationError):
        apply_online(admissions.row)
    login(erp.staff).post(f"/admissions/classes/{admissions.row.pk}/apply/", form_data())
    assert Application.objects.get().channel == "office"


def test_staff_correct_details_and_the_timeline_says_what(erp, admissions):
    application, _ = apply_online(admissions.row)
    client = login(erp.staff)
    response = client.post(f"/admissions/applications/{application.pk}/edit/", form_data(guardian_phone="01711000009"))
    assert response.status_code == 302
    application.refresh_from_db()
    assert application.guardian_phone == "01711000009"
    assert "guardian phone" in application.events.get(kind="details").text


def test_documents_are_checked_and_served_safely(erp, admissions, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    application, _ = apply_online(admissions.row)
    document = services.add_document(
        application=application,
        kind="birth_certificate",
        upload=SimpleUploadedFile("b.pdf", PDF, content_type="application/pdf"),
    )
    client = login(erp.staff)
    url = f"/admissions/applications/{application.pk}/"
    client.post(url, {"action": "reject_document", "document": document.pk})  # a reason is required
    document.refresh_from_db()
    assert document.status == "waiting"
    client.post(url, {"action": "reject_document", "document": document.pk, "reason": "Blurred; scan it again"})
    document.refresh_from_db()
    assert document.status == "rejected" and document.checked_by == erp.staff
    assert [kind for kind, _label in services.documents_missing(application)] == ["birth_certificate", "photo"]
    response = client.get(f"/admissions/documents/{document.pk}/")
    assert response["X-Content-Type-Options"] == "nosniff" and "sandbox" in response["Content-Security-Policy"]
    assert response["Cache-Control"] == "private, no-store"
    erp.other.admissions_enabled = True
    erp.other.save()
    outsider = User.objects.create_user(username="outsider", school=erp.other, password="Test-pass-9842")
    outsider.groups.add(Group.objects.get(name="Administrator"))
    assert login(outsider).get(f"/admissions/documents/{document.pk}/").status_code == 404


def test_a_sibling_is_confirmed_only_from_the_schools_current_students(erp, admissions):
    application, _ = apply_online(admissions.row, sibling_claimed="on", sibling_details="Ayesha, Class 1 A")
    services.confirm_sibling(user=erp.staff, application=application, student=erp.student)
    application.refresh_from_db()
    assert application.sibling == erp.student and application.sibling_verified_by == erp.staff
    from students.models import Student

    stranger = Student.objects.create(
        school=erp.other,
        student_id="X1",
        first_name="X",
        gender="F",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    with pytest.raises(PermissionDenied):
        services.confirm_sibling(user=erp.staff, application=application, student=stranger)
    erp.student.status = "withdrawn"
    erp.student.save()
    with pytest.raises(ValidationError):
        services.confirm_sibling(user=erp.staff, application=application, student=erp.student)


# ------------------------------------------------------------------ the fee


def test_the_fee_is_receipted_and_posted_to_admission_fee_income(erp, admissions):
    application, _ = apply_online(admissions.row)
    payment = services.record_payment(user=erp.accountant, application=application, amount="500", method="cash")
    assert payment.receipt_no == f"APF-{date.today().year}-0001"
    entry = payment.journal_entry
    assert entry.source == JournalEntry.Source.ADMISSION and entry.status == "posted"
    lines = {line.account.code: (line.debit, line.credit) for line in entry.lines.select_related("account")}
    assert lines == {"1010": (Decimal("500.00"), Decimal("0")), "4020": (Decimal("0"), Decimal("500.00"))}
    # The ledger names the receipt and the application, never the child.
    assert "Rafi" not in entry.narration and application.reference in entry.narration
    assert services.fee_state(application)["settled"]
    with pytest.raises(ValidationError, match="no more than"):
        services.record_payment(user=erp.accountant, application=application, amount="1", method="cash")
    with pytest.raises(ValidationError, match="application fee from the application"):
        reverse_journal(school=erp.school, user=erp.admin, entry=entry, reason="mistake")
    response = login(erp.accountant).get(f"/admissions/payments/{payment.pk}/receipt/")
    assert response["Content-Type"] == "application/pdf"


def test_a_void_reverses_the_entry_and_a_closed_period_refuses_both(erp, admissions):
    application, _ = apply_online(admissions.row)
    with pytest.raises(ValidationError, match="transaction ID"):
        services.record_payment(user=erp.accountant, application=application, amount="500", method="bank")
    payment = services.record_payment(
        user=erp.accountant, application=application, amount="200", method="mobile", reference="BK123"
    )
    with pytest.raises(ValidationError, match="reason"):
        services.void_payment(user=erp.accountant, payment=payment, reason=" ")
    services.void_payment(user=erp.accountant, payment=payment, reason="Wrong family")
    payment.refresh_from_db()
    assert payment.is_voided and payment.journal_entry.status == "reversed"
    assert services.fee_state(application)["outstanding"] == Decimal("500")
    erp.school.books_locked_until = date.today()
    erp.school.save()
    with pytest.raises(ValidationError, match="books are closed"):
        services.record_payment(user=erp.accountant, application=application, amount="500", method="cash")
    assert ApplicationPayment.objects.count() == 1


def test_a_waiver_needs_a_reason_and_no_payment(erp, admissions):
    application, _ = apply_online(admissions.row)
    with pytest.raises(PermissionDenied):
        services.waive_fee(user=erp.staff, application=application, reason="Staff child")
    with pytest.raises(ValidationError):
        services.waive_fee(user=erp.accountant, application=application, reason="")
    services.waive_fee(user=erp.accountant, application=application, reason="Staff child")
    application.refresh_from_db()
    state = services.fee_state(application)
    assert state["waived"] and state["settled"] and state["outstanding"] == 0
    with pytest.raises(ValidationError, match="waived"):
        services.record_payment(user=erp.accountant, application=application, amount="500", method="cash")


def test_the_front_office_cannot_take_money(erp, admissions):
    application, _ = apply_online(admissions.row)
    with pytest.raises(PermissionDenied):
        services.record_payment(user=erp.staff, application=application, amount="500", method="cash")
    login(erp.staff).post(
        f"/admissions/applications/{application.pk}/", {"action": "pay", "amount": "500", "method": "cash"}
    )
    assert not ApplicationPayment.objects.exists()


# ------------------------------------------------------------------ the link


def test_reissuing_the_link_is_on_the_timeline_and_the_audit_log(erp, admissions):
    from core.models import AuditLog

    application, raw = apply_online(admissions.row)
    login(erp.staff).post(f"/admissions/applications/{application.pk}/", {"action": "reissue"})
    application.refresh_from_db()
    assert application.token_generation == 2 and application.token_hash != services.hash_token(raw)
    assert application.events.filter(kind="link").exists()
    assert AuditLog.objects.filter(action="application.link_reissued", user=erp.staff).exists()


def test_the_pipeline_and_application_page_render(erp, admissions, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    first, _ = apply_online(admissions.row, sibling_claimed="on", sibling_details="Ayesha")
    apply_online(admissions.row)  # a duplicate of the first
    client = login(erp.admin)
    pipeline = client.get(f"/admissions/classes/{admissions.row.pk}/").content.decode()
    assert "Possible duplicate" in pipeline and "Sibling to check" in pipeline and "Rafi Ahmed" in pipeline
    page = client.get(f"/admissions/applications/{first.pk}/?sibling=S1").content.decode()
    assert "Ayesha" in page and "This one" in page and "Record a payment" in page
    home = client.get("/admissions/?q=APP-2027-0001").content.decode()
    assert f"/admissions/applications/{first.pk}/" in home
    assert client.get(f"/admissions/rounds/{admissions.round.pk}/").status_code == 200


def test_a_class_window_can_be_filled_from_ages(erp, admissions):
    from academics.models import ClassLevel

    level = ClassLevel.objects.create(school=erp.school, name="Class 2", order=2)
    response = login(erp.admin).post(
        f"/admissions/rounds/{admissions.round.pk}/classes/new/",
        {
            "class_level": level.pk,
            "seats": 30,
            "assessment": "none",
            "youngest": 7,
            "oldest": 8,
            "ages_on": "2027-01-01",
            "required_documents": ["birth_certificate"],
        },
    )
    assert response.status_code == 302
    row = admissions.round.classes.get(class_level=level)
    assert (row.born_on_or_after, row.born_on_or_before) == (date(2018, 1, 2), date(2020, 1, 1))
    assert row.required_documents == ["birth_certificate"]
    again = login(erp.admin).post(
        f"/admissions/rounds/{admissions.round.pk}/classes/new/",
        {"class_level": level.pk, "seats": 5, "assessment": "none"},
    )
    assert "already admits" in again.content.decode()


def test_the_family_link_documents_never_leave_the_school(erp, admissions):
    application, _ = apply_online(admissions.row)
    assert Client().get(f"/admissions/applications/{application.pk}/").status_code == 302
    assert ApplicationDocument.objects.count() == 0
