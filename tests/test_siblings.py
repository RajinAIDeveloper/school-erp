"""
One guardian, several children at the same school.

This is the ordinary case in Bangladesh, not an edge case, so every place a guardian's
children matter is checked: who they can see, what they are told, what they pay, and
that they get one login rather than one per child.
"""

from datetime import date
from decimal import Decimal

from django.test import Client

from academics.models import ClassLevel, Section
from attendance.services import save_register
from fees.models import FeeConcession, FeeInvoice
from fees.services import generate_invoices, sibling_discount_for
from messaging.models import SMSMessage
from students.models import Enrollment, Guardian, Student, StudentGuardian
from users.services import provision_section

DAY = date(2026, 9, 21)


def add_child(erp, name, section, roll, student_id, born=date(2017, 6, 1), guardian=None):
    student = Student.objects.create(
        school=erp.school,
        student_id=student_id,
        first_name=name,
        gender="M",
        date_of_birth=born,
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school,
        student=student,
        academic_year=erp.year,
        class_level=section.class_level,
        section=section,
        roll_number=roll,
    )
    StudentGuardian.objects.create(
        student=student, guardian=guardian or erp.guardian, relation="father", is_primary=True
    )
    return student


def other_class(erp):
    level = ClassLevel.objects.create(school=erp.school, name="Class 5", order=5)
    return Section.objects.create(school=erp.school, class_level=level, name="A")


# ---------------------------------------------------------------- what the guardian sees


def test_a_guardian_sees_every_child_on_home_and_in_each_record(erp):
    bilal = add_child(erp, "Bilal", erp.section, 2, "SIB-1")
    chowdhury = add_child(erp, "Chowdhury", other_class(erp), 1, "SIB-2")
    client = Client()
    client.force_login(erp.parent)

    home = client.get("/").content
    for name in (b"Ayesha", b"Bilal", b"Chowdhury"):
        assert name in home
    for student in (erp.student, bilal, chowdhury):
        assert client.get(f"/students/{student.pk}/").status_code == 200


def test_the_family_portal_totals_fees_across_children(erp):
    add_child(erp, "Bilal", erp.section, 2, "SIB-1")
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    client = Client()
    client.force_login(erp.parent)
    body = client.get("/").content.decode()
    assert "Fees due across the family" in body
    assert "2,000.00" in body  # two children, 1000 each
    assert "Children at this school" in body


def test_a_single_child_family_is_not_shown_a_family_summary(erp):
    client = Client()
    client.force_login(erp.parent)
    assert b"Fees due across the family" not in client.get("/").content


def test_a_guardian_of_one_family_cannot_see_another_family(erp):
    stranger = Guardian.objects.create(school=erp.school, full_name="Other parent", phone="01712345600")
    theirs = add_child(erp, "Nobody", other_class(erp), 1, "OTH-1", guardian=stranger)
    client = Client()
    client.force_login(erp.parent)
    assert client.get(f"/students/{theirs.pk}/").status_code == 404
    assert b"Nobody" not in client.get("/").content


# --------------------------------------------------------------------- what they are told


def test_a_class_notice_names_each_sibling_separately(erp):
    """A guardian with two children in one class is told about both, not just the first."""
    add_child(erp, "Bilal", erp.section, 2, "SIB-1")
    client = Client()
    client.force_login(erp.admin)
    client.post(
        "/sms/compose/",
        {
            "title": "Exam notice",
            "recipients": "class",
            "section": erp.section.pk,
            "body": "Dear guardian, {student} of {class} has an exam on Sunday.",
        },
    )
    bodies = sorted(SMSMessage.objects.values_list("body", flat=True))
    assert len(bodies) == 2
    assert any("Ayesha" in body for body in bodies)
    assert any("Bilal" in body for body in bodies)
    assert all(message.phone == "8801712345679" for message in SMSMessage.objects.all())


def test_a_generic_notice_still_reaches_a_number_only_once(erp):
    """Without placeholders the two messages would be identical, so one is enough."""
    add_child(erp, "Bilal", erp.section, 2, "SIB-1")
    client = Client()
    client.force_login(erp.admin)
    client.post(
        "/sms/compose/",
        {
            "title": "Closure",
            "recipients": "class",
            "section": erp.section.pk,
            "body": "The school is closed on Thursday.",
        },
    )
    assert SMSMessage.objects.count() == 1


def test_absence_alerts_go_out_once_per_absent_child(erp, django_capture_on_commit_callbacks):
    add_child(erp, "Bilal", erp.section, 2, "SIB-1")
    erp.school.notify_absence_sms = True
    erp.school.save()
    enrollments = list(Enrollment.objects.filter(school=erp.school, section=erp.section))
    with django_capture_on_commit_callbacks(execute=True):
        save_register(
            school=erp.school,
            user=erp.admin,
            day=DAY,
            entries=[(e, "absent", "", None, None) for e in enrollments],
        )
    bodies = list(SMSMessage.objects.values_list("body", flat=True))
    assert len(bodies) == 2
    assert any("Ayesha" in body for body in bodies) and any("Bilal" in body for body in bodies)


# ---------------------------------------------------------------------- what they pay


def test_sibling_discount_is_off_until_a_school_switches_it_on(erp):
    add_child(erp, "Bilal", erp.section, 2, "SIB-1", born=date(2018, 1, 1))
    assert sibling_discount_for(erp.school, erp.student) == Decimal("0")
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    assert {invoice.total for invoice in FeeInvoice.objects.all()} == {Decimal("1000.00")}


def test_the_eldest_pays_in_full_and_younger_siblings_get_the_rate(erp):
    # Ayesha was born in 2016; Bilal is younger and Chowdhury younger still.
    bilal = add_child(erp, "Bilal", erp.section, 2, "SIB-1", born=date(2018, 1, 1))
    chowdhury = add_child(erp, "Chowdhury", erp.section, 3, "SIB-2", born=date(2019, 1, 1))
    erp.school.sibling_discount_percent = Decimal("25")
    erp.school.save()

    assert sibling_discount_for(erp.school, erp.student) == Decimal("0")
    assert sibling_discount_for(erp.school, bilal) == Decimal("25")
    assert sibling_discount_for(erp.school, chowdhury) == Decimal("25")

    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    totals = {invoice.student.first_name: invoice.total for invoice in FeeInvoice.objects.select_related("student")}
    assert totals == {
        "Ayesha": Decimal("1000.00"),
        "Bilal": Decimal("750.00"),
        "Chowdhury": Decimal("750.00"),
    }


def test_an_only_child_never_gets_the_sibling_rate(erp):
    erp.school.sibling_discount_percent = Decimal("25")
    erp.school.save()
    assert sibling_discount_for(erp.school, erp.student) == Decimal("0")


def test_a_withdrawn_sibling_no_longer_counts(erp):
    """When the elder child leaves, the younger becomes the eldest and pays in full."""
    bilal = add_child(erp, "Bilal", erp.section, 2, "SIB-1", born=date(2018, 1, 1))
    erp.school.sibling_discount_percent = Decimal("25")
    erp.school.save()
    assert sibling_discount_for(erp.school, bilal) == Decimal("25")

    erp.student.status = Student.Status.WITHDRAWN
    erp.student.save()
    assert sibling_discount_for(erp.school, bilal) == Decimal("0")


def test_a_manual_concession_stacks_on_top_of_the_sibling_rate(erp):
    """A younger sibling on a scholarship gets both, which is what schools actually do."""
    bilal = add_child(erp, "Bilal", erp.section, 2, "SIB-1", born=date(2018, 1, 1))
    erp.school.sibling_discount_percent = Decimal("20")
    erp.school.save()
    FeeConcession.objects.create(school=erp.school, student=bilal, percent=Decimal("50"), reason="Scholarship")
    generate_invoices(
        school=erp.school,
        user=erp.admin,
        academic_year=erp.year,
        class_level=erp.level,
        month=9,
        issue_date=date(2026, 9, 1),
        due_date=date(2026, 9, 10),
    )
    # 1000 less 20% is 800; 800 less 50% is 400.
    assert FeeInvoice.objects.get(student=bilal).total == Decimal("400.00")


def test_siblings_are_defined_by_the_primary_guardian_not_any_link(erp):
    """A grandparent listed as a secondary guardian on two cousins does not make them siblings."""
    grandparent = Guardian.objects.create(school=erp.school, full_name="Nani", phone="01712345601")
    cousin = add_child(
        erp,
        "Cousin",
        other_class(erp),
        1,
        "COU-1",
        born=date(2018, 1, 1),
        guardian=Guardian.objects.create(school=erp.school, full_name="Aunt", phone="01712345602"),
    )
    StudentGuardian.objects.create(student=erp.student, guardian=grandparent, relation="grandparent")
    StudentGuardian.objects.create(student=cousin, guardian=grandparent, relation="grandparent")
    erp.school.sibling_discount_percent = Decimal("25")
    erp.school.save()
    assert sibling_discount_for(erp.school, cousin) == Decimal("0")


def test_twins_get_a_stable_answer(erp):
    """Same birthdate: the tie breaks on student ID, so reruns never flip who pays in full."""
    twin = add_child(erp, "Twin", erp.section, 2, "S0", born=erp.student.date_of_birth)
    erp.school.sibling_discount_percent = Decimal("25")
    erp.school.save()
    first = (sibling_discount_for(erp.school, erp.student), sibling_discount_for(erp.school, twin))
    second = (sibling_discount_for(erp.school, erp.student), sibling_discount_for(erp.school, twin))
    assert first == second
    assert sorted(first) == [Decimal("0"), Decimal("25")]


# ------------------------------------------------------------------------ their login


def test_provisioning_a_section_gives_a_guardian_of_two_one_login(erp):
    add_child(erp, "Bilal", erp.section, 2, "SIB-1")
    erp.guardian.user = None
    erp.guardian.save()
    rows = provision_section(school=erp.school, user=erp.admin, section=erp.section)
    guardian_rows = [row for row in rows if row["role"] == "Guardian"]
    assert len(guardian_rows) == 1
    erp.guardian.refresh_from_db()
    assert erp.guardian.user is not None
