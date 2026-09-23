"""
Before publishing: the checklist that blocks a wrong result and flags what deserves a look,
and the exempt state for a paper the school has excused a student from.
"""

from decimal import Decimal

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from academics.models import Subject, SubjectTeacher
from examinations.checklist import blockers, publication_checklist
from examinations.models import ExamSchedule, Mark, PaperComponent, UnlockRequest
from examinations.services import MarkEntryError, build_result_sheet, publish_exam, save_mark, save_marks


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def second_paper(erp):
    science = Subject.objects.create(school=erp.school, name="Science", code="SCI")
    SubjectTeacher.objects.create(
        school=erp.school, teacher=erp.employee, section=erp.section, subject=science, academic_year=erp.year
    )
    return ExamSchedule.objects.create(
        school=erp.school, exam=erp.exam, class_level=erp.level, subject=science, full_marks=100, pass_marks=33
    )


# ============================================================ exempt


def test_an_exempt_paper_is_left_out_of_the_result_and_shown_on_the_card(erp, second_paper):
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=erp.enrollment, exempt=True)
    save_mark(user=erp.teacher, schedule=second_paper, enrollment=erp.enrollment, score=Decimal(80))
    row = build_result_sheet(erp.exam, erp.level)["rows"][0]
    assert row["complete"] and row["result"] == "PASS"
    assert (row["total"], row["full_total"]) == ("80.00", "100.00")
    assert [c["letter"] for c in row["cells"] if c.get("exempt")] == ["EX"]
    publish_exam(erp.exam, erp.admin)
    card = login(erp.admin).get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").content.decode()
    assert "Exempt" in card
    assert login(erp.admin).get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf").status_code == 200


def test_only_managers_exempt_and_a_teachers_save_keeps_the_exemption(erp):
    with pytest.raises(PermissionDenied):
        save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, exempt=True)
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=erp.enrollment, exempt=True)
    mark = Mark.objects.get()
    # The teacher's grid shows the box disabled, so it posts nothing for it; the stored
    # exemption stands and a blank row does not clear it.
    client = login(erp.teacher)
    client.post(
        "/exams/marks/",
        {
            "schedule": erp.schedule.pk,
            "section": erp.section.pk,
            f"{erp.enrollment.pk}-score": "",
            f"{erp.enrollment.pk}-version": mark.version,
        },
    )
    assert Mark.objects.get().is_exempt
    with pytest.raises(MarkEntryError) as caught:
        save_marks(
            user=erp.teacher,
            schedule=erp.schedule,
            section=erp.section,
            rows=[(erp.enrollment, Decimal(50), False, mark.version, None, False)],
        )
    assert "managers" in caught.value.errors[erp.enrollment.pk]


def test_a_manager_can_undo_an_exemption_from_the_grid(erp):
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=erp.enrollment, exempt=True)
    mark = Mark.objects.get()
    login(erp.admin).post(
        "/exams/marks/",
        {
            "schedule": erp.schedule.pk,
            "section": erp.section.pk,
            f"{erp.enrollment.pk}-score": "64",
            f"{erp.enrollment.pk}-version": mark.version,
        },
    )
    mark.refresh_from_db()
    assert not mark.is_exempt and mark.marks_obtained == Decimal(64)


def test_exempt_and_absent_cannot_both_be_true(erp):
    mark = Mark(school=erp.school, schedule=erp.schedule, enrollment=erp.enrollment, is_absent=True, is_exempt=True)
    with pytest.raises(ValidationError):
        mark.full_clean()


def test_exports_print_ex_for_an_exempt_paper(erp, second_paper):
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=erp.enrollment, exempt=True)
    save_mark(user=erp.teacher, schedule=second_paper, enrollment=erp.enrollment, score=Decimal(80))
    body = login(erp.admin).get(f"/exams/results/?exam={erp.exam.pk}&class_level={erp.level.pk}&format=csv")
    assert ",EX," in body.content.decode("utf-8-sig")


# ============================================================ the checklist


def test_a_missing_mark_blocks_publication_with_the_students_named(erp):
    assert any("missing a mark or absence (Ayesha)" in text for text in blockers(erp.exam))
    with pytest.raises(ValidationError):
        publish_exam(erp.exam, erp.admin)


def test_parts_that_do_not_add_up_block_publication(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    # Set up behind the parts screen's back, as a hand edit would.
    PaperComponent.objects.create(school=erp.school, schedule=erp.schedule, code="a", name="A", full_marks=60)
    assert any("parts do not add up" in text for text in blockers(erp.exam))


def test_subject_choice_problems_block_publication(erp, board):
    board.humanities.group = ""
    board.humanities.save(update_fields=["group"])
    assert any("choices that need fixing (Rupa)" in text for text in blockers(board.exam))


def test_warnings_do_not_block(erp, second_paper):
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=erp.enrollment, exempt=True)
    save_mark(user=erp.teacher, schedule=second_paper, enrollment=erp.enrollment, score=Decimal(80))
    UnlockRequest.objects.create(school=erp.school, schedule=second_paper, requested_by=erp.teacher, reason="x")
    items = publication_checklist(erp.exam)
    assert not [i for i in items if i["level"] == "block"]
    texts = " ".join(i["text"] for i in items)
    assert "1 paper(s) are marked exempt" in texts and "1 unlock request(s)" in texts
    publish_exam(erp.exam, erp.admin)


def test_the_exam_page_shows_the_checklist_to_those_who_publish(erp):
    page = login(erp.admin).get(f"/exams/{erp.exam.pk}/").content.decode()
    assert "Before publishing" in page and "Must fix:" in page
    assert "Before publishing" not in login(erp.teacher).get(f"/exams/{erp.exam.pk}/").content.decode()
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    assert "Everything checks out." in login(erp.admin).get(f"/exams/{erp.exam.pk}/").content.decode()


def test_a_student_exempt_from_every_paper_has_no_result_and_no_position(erp):
    save_mark(user=erp.admin, schedule=erp.schedule, enrollment=erp.enrollment, exempt=True)
    assert not blockers(erp.exam)
    row = build_result_sheet(erp.exam, erp.level)["rows"][0]
    assert row["complete"] and row["headline"] == "Exempt from every paper"
    assert row["result"] is None and row["rank"] is None
    publish_exam(erp.exam, erp.admin)
