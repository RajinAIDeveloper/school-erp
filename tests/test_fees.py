"""Invoicing, collection, dues chasing, late fees and printed fee documents."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from academics.models import ClassLevel, Section
from core.models import AuditLog
from fees.models import FeeCategory, FeeInvoice, FeePayment, FeeStructure
from fees.services import (
    apply_late_fees,
    cancel_invoice,
    collect_payment,
    create_invoice,
    generate_invoices,
    outstanding_invoices,
    send_due_reminders,
)
from messaging.models import SMSMessage
from students.models import Enrollment, Student


def pay(erp, invoice, amount=400):
    return collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=amount,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )


def test_invoice_list_totals_are_annotated_and_query_count_is_flat(admin_client, erp, django_assert_max_num_queries):
    for n in range(2, 20):
        student = Student.objects.create(
            school=erp.school,
            student_id=f"F{n}",
            first_name=f"Payer {n}",
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
            roll_number=n,
        )
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    assert FeeInvoice.objects.count() == 19
    with django_assert_max_num_queries(20):
        assert admin_client.get("/fees/").status_code == 200
    invoice = FeeInvoice.objects.with_totals().first()
    assert invoice.balance_amount == invoice.total


def test_adhoc_invoice_raises_lines_and_audits(erp):
    category = FeeCategory.objects.create(school=erp.school, name="Re-sit fee")
    invoice = create_invoice(
        school=erp.school,
        user=erp.admin,
        student=erp.student,
        enrollment=erp.enrollment,
        academic_year=erp.year,
        month=None,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 15),
        items=[(category, Decimal("250"))],
        notes="Second attempt",
    )
    assert invoice.total == Decimal("250")
    assert invoice.items.count() == 1
    assert AuditLog.objects.filter(action="invoice.created").exists()


def test_adhoc_invoice_needs_at_least_one_priced_line(erp):
    category = FeeCategory.objects.create(school=erp.school, name="Blank")
    with pytest.raises(ValidationError):
        create_invoice(
            school=erp.school,
            user=erp.admin,
            student=erp.student,
            enrollment=erp.enrollment,
            academic_year=erp.year,
            month=None,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 9, 15),
            items=[(category, Decimal("0"))],
        )
    assert not FeeInvoice.objects.exists()


def test_adhoc_invoice_screen_creates_from_a_formset(admin_client, erp):
    category = FeeCategory.objects.create(school=erp.school, name="Transport")
    response = admin_client.post(
        "/fees/new/",
        {
            "student": erp.student.pk,
            "issue_date": "2026-09-01",
            "due_date": "2026-09-20",
            "month": "",
            "discount": "0",
            "late_fee": "0",
            "notes": "",
            "items-TOTAL_FORMS": "4",
            "items-INITIAL_FORMS": "0",
            "items-MIN_NUM_FORMS": "0",
            "items-MAX_NUM_FORMS": "20",
            "items-0-category": category.pk,
            "items-0-description": "Bus",
            "items-0-amount": "600",
            "items-1-category": "",
            "items-1-description": "",
            "items-1-amount": "",
            "items-2-category": "",
            "items-2-description": "",
            "items-2-amount": "",
            "items-3-category": "",
            "items-3-description": "",
            "items-3-amount": "",
        },
    )
    assert response.status_code == 302
    assert FeeInvoice.objects.get().total == Decimal("600")


def test_invoice_cancel_requires_no_live_payments(erp, invoice):
    with pytest.raises(ValidationError):
        cancel_invoice(school=erp.school, user=erp.admin, invoice=invoice, reason="")
    pay(erp, invoice, 100)
    with pytest.raises(ValidationError):
        cancel_invoice(school=erp.school, user=erp.admin, invoice=invoice, reason="Raised in error")
    invoice.refresh_from_db()
    assert invoice.status != "cancelled"


def test_invoice_cancel_voids_an_unpaid_invoice(erp, invoice):
    cancel_invoice(school=erp.school, user=erp.admin, invoice=invoice, reason="Duplicate of INV-2")
    invoice.refresh_from_db()
    assert invoice.status == "cancelled"
    assert "Duplicate of INV-2" in invoice.notes
    assert AuditLog.objects.filter(action="invoice.cancelled").exists()
    assert not outstanding_invoices(erp.school).filter(pk=invoice.pk).exists()


def test_generating_for_every_class_reports_one_total(erp):
    second = ClassLevel.objects.create(school=erp.school, name="Class 2", order=2)
    section = Section.objects.create(school=erp.school, class_level=second, name="A")
    student = Student.objects.create(
        school=erp.school,
        student_id="C2-1",
        first_name="Second",
        gender="M",
        date_of_birth=date(2015, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school,
        student=student,
        academic_year=erp.year,
        class_level=second,
        section=section,
        roll_number=1,
    )
    FeeStructure.objects.create(
        school=erp.school, academic_year=erp.year, class_level=second, category=erp.category, amount=1200
    )
    created, _skipped = generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=None,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    assert created == 2
    assert {i.total for i in FeeInvoice.objects.all()} == {Decimal("1000.00"), Decimal("1200.00")}


def test_generating_for_every_class_needs_a_structure_somewhere(erp):
    FeeStructure.objects.all().delete()
    with pytest.raises(ValidationError):
        generate_invoices(
            school=erp.school,
            user=erp.admin,
            academic_year=erp.year,
            class_level=None,
            month=9,
            issue_date=date(2026, 9, 1),
            due_date=date(2026, 9, 10),
        )


def test_dues_report_lists_only_what_is_owed(admin_client, erp, invoice):
    body = admin_client.get("/fees/due/").content
    assert invoice.invoice_no.encode() in body
    pay(erp, invoice, 1000)
    assert invoice.invoice_no.encode() not in admin_client.get("/fees/due/").content


def test_dues_report_filters_and_exports(admin_client, erp, invoice):
    assert admin_client.get(f"/fees/due/?section={erp.section.pk}").status_code == 200
    assert invoice.invoice_no.encode() not in admin_client.get(f"/fees/due/?section={erp.other_section.pk}").content
    assert admin_client.get("/fees/due/?format=pdf").content.startswith(b"%PDF")
    assert "Days overdue" in admin_client.get("/fees/due/?format=csv").content.decode("utf-8-sig")


def test_reminders_are_queued_once_per_invoice_per_day(erp, invoice):
    with pytest.raises(ValidationError):
        send_due_reminders(school=erp.school, user=erp.admin, invoices=[invoice])

    erp.school.notify_due_sms = True
    erp.school.save()
    invoice.refresh_from_db()
    assert send_due_reminders(school=erp.school, user=erp.admin, invoices=[invoice]) == 1
    assert send_due_reminders(school=erp.school, user=erp.admin, invoices=[invoice]) == 0
    message = SMSMessage.objects.get()
    assert "Ayesha" in message.body and message.status == "queued"


def test_reminders_need_messaging_rights(erp, invoice):
    erp.school.notify_due_sms = True
    erp.school.save()
    with pytest.raises(PermissionDenied):
        send_due_reminders(school=erp.school, user=erp.teacher, invoices=[invoice])


def test_late_fee_is_recomputed_not_stacked(erp, invoice):
    erp.school.late_fee_per_day = Decimal("10")
    erp.school.save()
    as_of = invoice.due_date + timedelta(days=5)
    assert apply_late_fees(school=erp.school, user=erp.admin, as_of=as_of) == 1
    invoice.refresh_from_db()
    assert invoice.late_fee == Decimal("50.00")

    # Running it again the same day changes nothing.
    assert apply_late_fees(school=erp.school, user=erp.admin, as_of=as_of) == 0
    invoice.refresh_from_db()
    assert invoice.late_fee == Decimal("50.00")
    assert invoice.total == Decimal("1050.00")


def test_late_fee_respects_the_cap_and_needs_a_rate(erp, invoice):
    with pytest.raises(ValidationError):
        apply_late_fees(school=erp.school, user=erp.admin)
    erp.school.late_fee_per_day = Decimal("10")
    erp.school.late_fee_cap = Decimal("30")
    erp.school.save()
    apply_late_fees(school=erp.school, user=erp.admin, as_of=invoice.due_date + timedelta(days=90))
    invoice.refresh_from_db()
    assert invoice.late_fee == Decimal("30.00")


def test_payment_confirmation_sms_when_enabled(erp, invoice, django_capture_on_commit_callbacks):
    erp.school.notify_payment_sms = True
    erp.school.save()
    with django_capture_on_commit_callbacks(execute=True):
        payment = pay(erp, invoice, 400)
    message = SMSMessage.objects.get()
    assert payment.receipt_no in message.body
    assert message.dedupe_key == f"payment:{payment.pk}"


def test_no_payment_sms_by_default(erp, invoice, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        pay(erp, invoice, 400)
    assert not SMSMessage.objects.exists()


def test_receipt_pdf_carries_the_school_and_amount_in_words(admin_client, erp, invoice):
    payment = pay(erp, invoice, 400)
    response = admin_client.get(f"/fees/receipts/{payment.pk}.pdf")
    assert response.status_code == 200 and response.content.startswith(b"%PDF")
    assert b"attachment" in response["Content-Disposition"].encode()
    duplicate = admin_client.get(f"/fees/receipts/{payment.pk}.pdf?copy=1")
    assert duplicate.content.startswith(b"%PDF")


def test_amount_in_words_reads_the_way_a_receipt_should():
    from core.money import amount_in_words

    assert amount_in_words(Decimal("150000.50")) == "One Lakh Fifty Thousand Taka and Fifty Paisa Only"
    assert amount_in_words(Decimal("1200")) == "One Thousand Two Hundred Taka Only"
    assert amount_in_words(Decimal("0")) == "Zero Taka Only"
    assert amount_in_words(Decimal("12345678.09")).startswith("One Crore Twenty Three Lakh")


def test_statement_running_balance_matches_charges_less_receipts(admin_client, erp, invoice):
    pay(erp, invoice, 400)
    body = admin_client.get(f"/fees/statement/{erp.student.pk}/").content.decode()
    assert "600" in body
    assert admin_client.get(f"/fees/statement/{erp.student.pk}/?format=pdf").content.startswith(b"%PDF")


def test_parent_sees_only_their_own_statement(erp, invoice):
    other = Student.objects.create(
        school=erp.school,
        student_id="S-OTHER",
        first_name="Private",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    client = Client()
    client.force_login(erp.parent)
    assert client.get(f"/fees/statement/{erp.student.pk}/").status_code == 200
    assert client.get(f"/fees/statement/{other.pk}/").status_code == 404
    assert client.get("/fees/due/").status_code == 403


def test_cancelled_payment_leaves_the_invoice_owing(erp, invoice):
    payment = pay(erp, invoice, 1000)
    invoice.refresh_from_db()
    assert invoice.status == "paid"
    from fees.services import cancel_payment

    cancel_payment(school=erp.school, user=erp.accountant, payment=payment, reason="Cheque bounced")
    invoice.refresh_from_db()
    assert invoice.balance == Decimal("1000.00") and invoice.status == "unpaid"
    assert FeePayment.objects.get().is_cancelled
