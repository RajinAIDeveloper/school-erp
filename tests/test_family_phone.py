"""
The family screens on a phone: the Pay button on the home card, the fees page opening on the
child who owes, and tables that stack instead of hiding the balance off the side of the screen.
"""

from datetime import date
from pathlib import Path

import pytest
from django.test import Client

from academics.models import ClassLevel, Section
from students.models import Enrollment, Student, StudentGuardian

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def two_children(erp, invoice):
    """Ayesha (S1) owes the September invoice; her brother A0 sorts first and owes nothing."""
    level = ClassLevel.objects.create(school=erp.school, name="Class 3", order=3)
    section = Section.objects.create(school=erp.school, class_level=level, name="A")
    brother = Student.objects.create(
        school=erp.school,
        student_id="A0",
        first_name="Rafi",
        gender="M",
        date_of_birth=date(2018, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school, student=brother, academic_year=erp.year, class_level=level, section=section, roll_number=1
    )
    StudentGuardian.objects.create(student=brother, guardian=erp.guardian, relation="mother")
    client = Client()
    client.force_login(erp.parent)
    return brother, client


def test_the_fees_page_opens_on_the_child_who_owes(erp, two_children):
    brother, client = two_children
    response = client.get("/portal/fees/")
    assert response.context["selected"] == erp.student
    assert response.context["students"][0] == brother  # first on the list, but owes nothing
    # Choosing a child still shows that child.
    assert client.get(f"/portal/fees/?student={brother.pk}").context["selected"] == brother


def test_the_home_card_offers_to_pay_for_the_child_who_owes(erp, two_children):
    brother, client = two_children
    erp.school.payment_gateway = "demo"
    erp.school.save()
    body = client.get("/portal/").content.decode()
    assert f'href="/portal/fees/?student={erp.student.pk}">Pay fees' in body
    assert body.count("Pay fees") == 1  # not on the brother's card

    erp.school.payment_gateway = "none"
    erp.school.save()
    body = client.get("/portal/").content.decode()
    assert "Pay fees" not in body and "See fees due" in body


def test_family_tables_stack_on_a_phone(erp, two_children):
    _brother, client = two_children
    body = client.get(f"/portal/fees/?student={erp.student.pk}").content.decode()
    assert 'class="table table-stack"' in body
    assert 'data-label="Balance"' in body
    assert "10 Sep 2026" in body  # the due date, in the same form as every other date
    # The built stylesheet carries the phone layout, not just the source.
    assert ".table-stack thead{display:none}" in (ROOT / "static/css/app.css").read_text(encoding="utf-8")
