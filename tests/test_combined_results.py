"""
Combined results: published exams weighted into one result, published as its own version,
and marked out of date when a source exam is corrected afterwards.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from core.models import AssessmentSystem
from examinations.combined import live_combined_sheet, out_of_date, publish_combined, save_combined
from examinations.models import CombinedSnapshot, Exam, ExamSchedule, UnlockRequest
from examinations.services import publish_exam, review_unlock, save_mark


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def final(erp):
    exam = Exam.objects.create(school=erp.school, academic_year=erp.year, name="Final", grade_scale=erp.scale)
    schedule = ExamSchedule.objects.create(
        school=erp.school, exam=exam, class_level=erp.level, subject=erp.subject, full_marks=50, pass_marks=17
    )
    return exam, schedule


def sit(erp, final, first=None, second=None, *, absent=False, exempt=False):
    exam, schedule = final
    save_mark(
        user=erp.admin,
        schedule=erp.schedule,
        enrollment=erp.enrollment,
        score=None if (absent or exempt) else Decimal(first),
        absent=absent,
        exempt=exempt,
    )
    save_mark(user=erp.admin, schedule=schedule, enrollment=erp.enrollment, score=Decimal(second))
    publish_exam(erp.exam, erp.admin)
    publish_exam(exam, erp.admin)


def annual(erp, final, weights=(30, 70)):
    return save_combined(
        user=erp.admin,
        school=erp.school,
        academic_year=erp.year,
        name="Annual result",
        grade_scale=erp.scale,
        parts=[(erp.exam, weights[0]), (final[0], weights[1])],
    )


# ============================================================ the arithmetic


def test_each_subject_is_the_weighted_average_of_its_percentages(erp, final):
    sit(erp, final, 60, 40)  # 60% and 80% (40 of 50)
    row = live_combined_sheet(annual(erp, final), erp.level)[0]
    cell = row["cells"][0]
    assert cell["score"] == "74.00"  # 30% of 60 + 70% of 80
    assert row["result"] == "PASS" and row["percent"] == "74.00"
    assert any("Term 1 60.00% × 30.00%" in line for line in row["trace"])
    assert [s["exam"] for s in row["sources"]] == ["Term 1", "Final"]


def test_an_absence_counts_as_nought_for_its_exam(erp, final):
    sit(erp, final, absent=True, second=40)
    assert live_combined_sheet(annual(erp, final), erp.level)[0]["cells"][0]["score"] == "56.00"


def test_an_exemption_leaves_that_exam_out_of_the_average(erp, final):
    sit(erp, final, exempt=True, second=40)
    assert live_combined_sheet(annual(erp, final), erp.level)[0]["cells"][0]["score"] == "80.00"


def test_the_pass_mark_is_weighted_too(erp, final):
    # 33% and 34% pass marks weighted 30/70 give 33.70%: 33.5% fails.
    sit(erp, final, 35, 16.5)  # 35% and 33%
    row = live_combined_sheet(annual(erp, final), erp.level)[0]
    assert row["cells"][0]["score"] == "33.60"
    assert row["result"] == "FAIL"


# ============================================================ setting up


@pytest.mark.parametrize(
    "weights,message",
    [((30, 60), "add up to 90"), ((0, 100), "add up to 100"), ((50, 50), None)],
)
def test_weights_must_add_up_to_a_hundred(erp, final, weights, message):
    if message is None:
        annual(erp, final, weights)
        return
    with pytest.raises(ValidationError) as caught:
        annual(erp, final, weights)
    assert "100%" in " ".join(caught.value.messages)


def test_a_combined_result_needs_published_exams_and_one_rulebook(erp, final):
    combined = annual(erp, final)
    with pytest.raises(ValidationError) as caught:
        live_combined_sheet(combined, erp.level)
    assert "Publish these exams first" in " ".join(caught.value.messages)
    sit(erp, final, 60, 40)
    Exam.objects.filter(pk=final[0].pk).update(assessment_system=AssessmentSystem.CAMBRIDGE)
    with pytest.raises(ValidationError) as caught:
        live_combined_sheet(combined, erp.level)
    assert "different rulebooks" in " ".join(caught.value.messages)


def test_only_managers_set_up_and_publish(erp, final):
    with pytest.raises(PermissionDenied):
        save_combined(
            user=erp.teacher,
            school=erp.school,
            academic_year=erp.year,
            name="Annual",
            grade_scale=erp.scale,
            parts=[(erp.exam, 30), (final[0], 70)],
        )
    sit(erp, final, 60, 40)
    combined = annual(erp, final)
    with pytest.raises(PermissionDenied):
        publish_combined(combined, erp.teacher)
    assert login(erp.teacher).get("/exams/combined/").status_code == 403


# ============================================================ publishing and corrections


def test_publishing_freezes_the_result_and_a_correction_marks_it_out_of_date(erp, final):
    sit(erp, final, 60, 40)
    combined = publish_combined(annual(erp, final), erp.admin)
    first = CombinedSnapshot.objects.get(version=1)
    assert first.payload["cells"][0]["score"] == "74.00" and first.payload["fingerprint"]
    with pytest.raises(ValidationError):
        publish_combined(combined, erp.admin)

    exam, schedule = final
    unlock = UnlockRequest.objects.create(school=erp.school, schedule=schedule, requested_by=erp.admin, reason="Typo")
    review_unlock(unlock, erp.admin, True)
    save_mark(user=erp.admin, schedule=schedule, enrollment=erp.enrollment, score=Decimal(45), expected_version=1)
    combined.refresh_from_db()
    assert out_of_date(combined) == ["Final"]
    # The published combined result is unchanged until someone publishes it again.
    card = login(erp.admin).get(f"/exams/combined/{combined.pk}/card/{erp.student.pk}/").content.decode()
    assert "74.00" in card and "Corrected since this was published" in card
    publish_combined(combined, erp.admin)
    combined.refresh_from_db()
    assert out_of_date(combined) == []
    assert CombinedSnapshot.objects.get(version=2).payload["cells"][0]["score"] == "81.00"
    assert CombinedSnapshot.objects.get(version=1).payload["cells"][0]["score"] == "74.00"


# ============================================================ cards, families, verification


def test_the_family_sees_the_published_combined_card_and_it_verifies(erp, final):
    sit(erp, final, 60, 40)
    combined = publish_combined(annual(erp, final), erp.admin)
    family = login(erp.parent)
    portal = family.get(f"/portal/results/?student={erp.student.pk}").content.decode()
    assert "Annual result" in portal
    card = family.get(f"/exams/combined/{combined.pk}/card/{erp.student.pk}/").content.decode()
    assert "Combined from" in card and "Term 1 30%" in card and "Final 70%" in card
    assert "Corrected since" not in card
    assert family.get(f"/exams/combined/{combined.pk}/card/{erp.student.pk}/?format=pdf").status_code == 200
    snapshot = CombinedSnapshot.objects.get()
    check = Client().get(f"/exams/combined/verify/{snapshot.verification_code}/").content.decode()
    assert "current, published report card" in check and snapshot.payload["fingerprint"] in check
    assert "Ayesha" not in check


def test_a_draft_combined_card_is_for_staff_only(erp, final):
    sit(erp, final, 60, 40)
    combined = annual(erp, final)
    assert login(erp.parent).get(f"/exams/combined/{combined.pk}/card/{erp.student.pk}/").status_code == 403
    assert login(erp.admin).get(f"/exams/combined/{combined.pk}/card/{erp.student.pk}/").status_code == 200


def test_the_screens_set_up_publish_and_download(erp, final):
    sit(erp, final, 60, 40)
    client = login(erp.admin)
    response = client.post(
        "/exams/combined/",
        {
            "academic_year": erp.year.pk,
            "name": "Annual result",
            "grade_scale": erp.scale.pk,
            f"weight-{erp.exam.pk}": "30",
            f"weight-{final[0].pk}": "70",
        },
    )
    assert response.status_code == 302
    detail = response["Location"]
    assert b"74.00" in client.get(detail).content
    client.post(detail)
    assert CombinedSnapshot.objects.count() == 1
    csv = client.get(detail + f"?class_level={erp.level.pk}&format=csv").content.decode("utf-8-sig")
    assert "Published version 1" in csv and "Term 1 30.00%" in csv
    bad = client.post(
        "/exams/combined/",
        {"academic_year": erp.year.pk, "name": "Other", "grade_scale": erp.scale.pk, f"weight-{erp.exam.pk}": "30"},
    )
    assert b"at least two exams" in bad.content
