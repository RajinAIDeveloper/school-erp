"""
What a report card says beyond marks: subject comments and effort, the class teacher's
comment, the school's grade estimates, and the attendance cut-off. All frozen at publication.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.utils import timezone

from academics.models import ClassLevel, Section, Subject
from core.models import AssessmentSystem
from examinations.feedback import (
    CommentError,
    approve_forecasts,
    card_feedback,
    record_forecasts,
    save_overall_comments,
    save_subject_comments,
)
from examinations.models import GradeForecast, ResultComment, ResultSnapshot
from examinations.services import build_result_sheet, publish_exam, save_mark


def login(user):
    client = Client()
    client.force_login(user)
    return client


def card_of(client, erp):
    page = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").content
    return page[page.index(b"<article") : page.index(b"</article>")]


def comment(erp, text, effort="A", user=None):
    return save_subject_comments(
        user=user or erp.teacher, schedule=erp.schedule, section=erp.section, rows=[(erp.enrollment, effort, text)]
    )


def make_class_teacher(erp):
    erp.section.class_teacher = erp.employee
    erp.section.save(update_fields=["class_teacher"])


# ============================================================ subject comments


def test_a_teacher_comments_on_their_own_subject_and_it_reaches_the_card(erp):
    assert comment(erp, "Careful, steady work.") == 1
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    card = card_of(login(erp.admin), erp)
    assert b"Careful, steady work." in card
    assert b"Effort" in card


def test_saving_the_same_comment_again_changes_nothing_and_blank_removes_it(erp):
    comment(erp, "Good.")
    assert comment(erp, "Good.") == 0
    assert comment(erp, "", effort="") == 1
    assert not ResultComment.objects.exists()


def test_a_teacher_cannot_comment_on_another_section(erp):
    with pytest.raises(PermissionDenied):
        save_subject_comments(
            user=erp.teacher, schedule=erp.schedule, section=erp.other_section, rows=[(erp.enrollment, "", "x")]
        )


def test_a_student_from_another_section_is_refused(erp):
    erp.enrollment.section = erp.other_section
    with pytest.raises(ValidationError):
        comment(erp, "Moved", user=erp.admin)


def test_an_overlong_comment_is_refused_against_its_student(erp):
    with pytest.raises(CommentError) as caught:
        comment(erp, "x" * 601)
    assert erp.enrollment.pk in caught.value.errors


def test_comments_are_fixed_once_results_are_published_and_frozen_on_the_card(erp):
    comment(erp, "Before publication.")
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    with pytest.raises(ValidationError):
        comment(erp, "After publication.")
    # Even a change made behind the screen does not reach the published card.
    ResultComment.objects.update(comment="Changed in the database.")
    card = card_of(login(erp.admin), erp)
    assert b"Before publication." in card and b"Changed in the database." not in card
    payload = ResultSnapshot.objects.get().payload
    assert payload["comments"][str(erp.subject.pk)]["comment"] == "Before publication."


def test_the_comments_screen_saves_and_keeps_typed_text_on_error(erp):
    client = login(erp.teacher)
    url = f"/exams/comments/?schedule={erp.schedule.pk}&section={erp.section.pk}"
    assert client.get(url).status_code == 200
    pk = erp.enrollment.pk
    bad = client.post(
        "/exams/comments/",
        {"schedule": erp.schedule.pk, "section": erp.section.pk, f"{pk}-effort": "B", f"{pk}-comment": "y" * 601},
    )
    assert bad.status_code == 200 and b"Nothing was saved" in bad.content
    assert b'value="B"' in bad.content
    good = client.post(
        "/exams/comments/",
        {"schedule": erp.schedule.pk, "section": erp.section.pk, f"{pk}-effort": "B", f"{pk}-comment": "Fine."},
    )
    assert good.status_code == 302
    assert ResultComment.objects.get().comment == "Fine."
    other = f"/exams/comments/?schedule={erp.schedule.pk}&section={erp.other_section.pk}"
    # A section the teacher does not teach is not offered, so no grid is shown for it.
    assert b"Save comments" not in client.get(other).content


def test_an_ib_class_calls_effort_approaches_to_learning(erp):
    level = ClassLevel.objects.create(
        school=erp.school, name="MYP 5", order=11, assessment_system=AssessmentSystem.IB_MYP
    )
    Section.objects.create(school=erp.school, class_level=level, name="A")
    from examinations.feedback import effort_label

    assert effort_label(erp.exam.rules_for(level)) == "ATL"
    assert effort_label(erp.exam.rules_for(erp.level)) == "Effort"


# ============================================================ overall comments


def test_only_the_class_teacher_or_a_manager_writes_the_overall_comment(erp):
    rows = [(erp.enrollment, "", "A good term.")]
    with pytest.raises(PermissionDenied):
        save_overall_comments(user=erp.teacher, exam=erp.exam, section=erp.section, rows=rows)
    make_class_teacher(erp)
    assert save_overall_comments(user=erp.teacher, exam=erp.exam, section=erp.section, rows=rows) == 1
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    card = card_of(login(erp.admin), erp)
    assert b"Class teacher&#x27;s comment" in card or b"Class teacher's comment" in card
    assert b"A good term." in card


def test_the_overall_comments_screen(erp):
    url = f"/exams/comments/?overall=1&exam={erp.exam.pk}&section={erp.section.pk}"
    assert login(erp.teacher).get(url).status_code == 403
    make_class_teacher(erp)
    client = login(erp.teacher)
    assert client.get(url).status_code == 200
    response = client.post(
        "/exams/comments/",
        {"overall": "1", "exam": erp.exam.pk, "section": erp.section.pk, f"{erp.enrollment.pk}-comment": "Well done."},
    )
    assert response.status_code == 302
    assert ResultComment.objects.get(subject__isnull=True).comment == "Well done."


# ============================================================ grade estimates


def estimate(erp, grade, *, user=None, kind="predicted", as_of=None):
    return record_forecasts(
        user=user or erp.teacher,
        section=erp.section,
        subject=erp.subject,
        academic_year=erp.year,
        kind=kind,
        as_of=as_of or timezone.localdate(),
        rows=[(erp.enrollment, grade)],
    )


def test_a_teachers_estimate_waits_for_approval_before_any_card_shows_it(erp):
    erp.exam.end_date = timezone.localdate()
    erp.exam.save()
    estimate(erp, "A")
    record = GradeForecast.objects.get()
    assert record.approved_at is None
    assert card_feedback(erp.exam, [erp.enrollment.pk], erp.exam.end_date)[erp.enrollment.pk]["forecasts"] == []
    with pytest.raises(PermissionDenied):
        approve_forecasts(
            user=erp.teacher, section=erp.section, subject=erp.subject, academic_year=erp.year, kind="predicted"
        )
    assert (
        approve_forecasts(
            user=erp.admin, section=erp.section, subject=erp.subject, academic_year=erp.year, kind="predicted"
        )
        == 1
    )
    shown = card_feedback(erp.exam, [erp.enrollment.pk], erp.exam.end_date)[erp.enrollment.pk]["forecasts"]
    assert [(f["kind"], f["grade"]) for f in shown] == [("predicted", "A")]


def test_estimates_keep_their_history_and_the_card_shows_the_latest_up_to_the_exam(erp):
    today = timezone.localdate()
    estimate(erp, "B", user=erp.admin, as_of=today - timedelta(days=30))
    estimate(erp, "A", user=erp.admin, as_of=today - timedelta(days=5))
    estimate(erp, "A*", user=erp.admin, as_of=today)
    assert GradeForecast.objects.count() == 3
    exam_day = today - timedelta(days=2)
    shown = card_feedback(erp.exam, [erp.enrollment.pk], exam_day)[erp.enrollment.pk]["forecasts"]
    # The A* was decided after the exam, so this exam's card shows the A.
    assert [f["grade"] for f in shown] == ["A"]


def test_estimates_are_labelled_as_the_schools_own_on_the_card(erp):
    erp.exam.end_date = timezone.localdate()
    erp.exam.save()
    estimate(erp, "7", user=erp.admin)
    estimate(erp, "6", user=erp.admin, kind="target")
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    card = card_of(login(erp.admin), erp)
    assert b"not a result awarded by an examination board" in card
    assert b"Predicted grade" in card and b"Target grade" in card


def test_estimates_are_refused_for_a_subject_the_teacher_does_not_teach_there(erp):
    other = Subject.objects.create(school=erp.school, name="Art", code="ART")
    with pytest.raises(PermissionDenied):
        record_forecasts(
            user=erp.teacher,
            section=erp.section,
            subject=other,
            academic_year=erp.year,
            kind="predicted",
            as_of=date.today(),
            rows=[(erp.enrollment, "A")],
        )


def test_an_estimate_cannot_be_dated_in_the_future_or_be_an_unknown_kind(erp):
    with pytest.raises(ValidationError):
        estimate(erp, "A", as_of=timezone.localdate() + timedelta(days=1))
    with pytest.raises(ValidationError):
        estimate(erp, "A", kind="guess")
    with pytest.raises(ValidationError):
        estimate(erp, "TOOLONG")


def test_the_estimates_screen_records_and_a_manager_approves(erp):
    teacher = login(erp.teacher)
    url = f"/exams/estimates/?section={erp.section.pk}&subject={erp.subject.pk}&kind=predicted"
    assert teacher.get(url).status_code == 200
    response = teacher.post(
        "/exams/estimates/",
        {
            "section": erp.section.pk,
            "subject": erp.subject.pk,
            "kind": "predicted",
            "action": "record",
            "as_of": timezone.localdate().isoformat(),
            f"{erp.enrollment.pk}-grade": "B",
        },
    )
    assert response.status_code == 302
    admin = login(erp.admin)
    assert b"waiting for approval" in admin.get(url).content
    admin.post(
        "/exams/estimates/",
        {"section": erp.section.pk, "subject": erp.subject.pk, "kind": "predicted", "action": "approve"},
    )
    assert GradeForecast.objects.get().approved_by == erp.admin


# ============================================================ attendance and the PDF


def test_the_card_states_the_attendance_cut_off(erp):
    from attendance.models import StudentAttendance

    erp.exam.end_date = date(2026, 9, 25)
    erp.exam.save()
    StudentAttendance.objects.create(
        school=erp.school, enrollment=erp.enrollment, date=date(2026, 9, 21), status="present"
    )
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    card = card_of(login(erp.admin), erp)
    assert b"25 Sep 2026" in card


def test_the_pdf_card_carries_comments_and_estimates(erp):
    erp.exam.end_date = timezone.localdate()
    erp.exam.save()
    comment(erp, "Strong analysis.")
    estimate(erp, "A", user=erp.admin)
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    row = build_result_sheet(erp.exam, erp.level, erp.section)["rows"][0]
    assert row["comments"][str(erp.subject.pk)]["comment"] == "Strong analysis."
    response = login(erp.admin).get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"


def test_a_card_published_before_comments_existed_still_renders(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    snapshot = ResultSnapshot.objects.get()
    payload = dict(snapshot.payload)
    for key in ("comments", "overall_comment", "forecasts", "effort_label"):
        payload.pop(key)
    payload["attendance"] = None
    ResultSnapshot.objects.filter(pk=snapshot.pk).update(payload=payload)
    client = login(erp.admin)
    assert client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").status_code == 200
    assert client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf").status_code == 200
