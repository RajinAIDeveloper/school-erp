"""
VAT per fee head: none unless the school sets a rate, worked out on each invoice line when it
is written, included in totals, and credited to VAT payable in the same share as it is paid.
"""

from datetime import date
from decimal import Decimal

from django.test import Client

from fees.models import FeeInvoice
from fees.services import collect_payment, generate_invoices
from finance.models import Account


def generate(erp):
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    return FeeInvoice.objects.get()


def balance(erp, code):
    return Account.objects.get(school=erp.school, code=code).balance()


def pay(erp, invoice, amount):
    return collect_payment(
        school=erp.school,
        user=erp.accountant,
        invoice=invoice,
        amount=amount,
        method="cash",
        reference="",
        date=date(2026, 9, 21),
    )


def test_no_vat_unless_the_school_sets_a_rate(erp):
    invoice = generate(erp)
    assert invoice.vat_total == 0 and invoice.total == Decimal("1000.00")
    pay(erp, invoice, 1000)
    assert balance(erp, "2020") == 0


def test_vat_is_added_to_the_invoice_and_its_share_is_owed_not_earned(erp):
    erp.category.vat_rate = Decimal("5")
    erp.category.save()
    invoice = generate(erp)
    assert invoice.items.get().vat == Decimal("50.00")
    assert invoice.total == Decimal("1050.00")
    annotated = FeeInvoice.objects.with_totals().get(pk=invoice.pk)
    assert (annotated.vat_amount, annotated.total_amount, annotated.balance_amount) == (
        Decimal("50.00"),
        Decimal("1050.00"),
        Decimal("1050.00"),
    )
    pay(erp, invoice, 525)  # half
    assert balance(erp, "2020") == Decimal("25.00")
    pay(erp, invoice, 525)
    invoice.refresh_from_db()
    assert invoice.status == "paid"
    assert balance(erp, "2020") == Decimal("50.00")
    assert balance(erp, "1010") == Decimal("1050.00")


def test_a_later_rate_change_leaves_issued_invoices_alone(erp):
    erp.category.vat_rate = Decimal("5")
    erp.category.save()
    invoice = generate(erp)
    erp.category.vat_rate = Decimal("15")
    erp.category.save()
    invoice.refresh_from_db()
    assert invoice.total == Decimal("1050.00")


def test_the_invoice_shows_its_vat(erp):
    erp.category.vat_rate = Decimal("5")
    erp.category.save()
    invoice = generate(erp)
    client = Client()
    client.force_login(erp.admin)
    page = client.get(f"/fees/{invoice.pk}/").content.decode()
    assert "VAT" in page and "1,050" in page


def test_vat_is_charged_on_the_fee_after_an_invoice_discount(erp):
    from fees.services import create_invoice, edit_invoice

    erp.category.vat_rate = Decimal("5")
    erp.category.save()
    invoice = create_invoice(
        school=erp.school,
        user=erp.admin,
        student=erp.student,
        enrollment=erp.enrollment,
        academic_year=erp.year,
        month=None,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
        items=[(erp.category, "1000")],
        discount="200",
    )
    assert invoice.vat_total == Decimal("40.00")  # 5% of the 800 actually charged
    assert invoice.total == Decimal("840.00")
    edit_invoice(school=erp.school, user=erp.admin, invoice=invoice, items=[(erp.category, "1000")], discount="0")
    invoice.refresh_from_db()
    assert invoice.vat_total == Decimal("50.00") and invoice.total == Decimal("1050.00")
