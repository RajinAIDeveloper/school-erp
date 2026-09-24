"""
Result-day lists and the class's highest marks: who failed and in how many subjects, who is
within a few marks of the pass mark, and the highest mark in each subject on the card.
"""

from django.test import Client

from core.pdf import styles
from examinations.documents import report_card_flowables
from examinations.models import ResultSnapshot
from examinations.services import build_result_sheet, publish_exam
from tests.test_board_results import mark_everything
from tests.test_rulebooks import card_texts


def login(user):
    client = Client()
    client.force_login(user)
    return client


def results_url(exam, level, **extra):
    query = "&".join(f"{k}={v}" for k, v in {"exam": exam.pk, "class_level": level.pk, **extra}.items())
    return f"/exams/results/?{query}"


def scores(board, **by_code):
    """{(enrollment, paper code): score}: Nabila (science) first, then Rupa (humanities)."""
    found = {}
    for code, pair in by_code.items():
        for enrollment, value in zip((board.science, board.humanities), pair, strict=True):
            if value is not None:
                found[(enrollment.pk, code)] = value
    return found


def test_the_failed_list_counts_main_subjects_and_names_them(erp, board):
    # Nabila fails General Math and Physics, and her 4th subject, which does not count.
    mark_everything(erp, board, scores(board, **{"109": (20, 85), "136": (30, None), "126": (10, None)}))
    client = login(erp.admin)
    body = client.get(results_url(board.exam, board.level, report="fails")).content.decode()
    assert "Failed subjects" in body
    assert "1 of 2 student(s) failed at least one subject (1 in 2 subjects)" in body
    assert "Nabila" in body and "Rupa" not in body
    assert "Physics" in body and "Higher" not in body  # the 4th subject is left out
    csv = client.get(results_url(board.exam, board.level, report="fails", format="csv")).content.decode("utf-8-sig")
    assert "Subjects failed,Section,Roll,Student,Group,Failed in" in csv


def test_the_near_pass_list_shows_scores_just_either_side(erp, board):
    # Rupa passes Geography by 2; Nabila is 3 short in Physics and 13 short in Math.
    mark_everything(erp, board, scores(board, **{"110": (None, 35), "136": (30, None), "109": (20, 85)}))
    client = login(erp.admin)
    body = client.get(results_url(board.exam, board.level, report="nearfail")).content.decode()
    assert "Passed by 2" in body and "Short by 3" in body
    assert "Short by 13" not in body
    wider = client.get(results_url(board.exam, board.level, report="nearfail", margin=15)).content.decode()
    assert "Short by 13" in wider


def test_a_rulebook_without_pass_marks_has_no_fail_lists(erp, igcse):
    client = login(erp.admin)
    for kind in ("fails", "nearfail"):
        body = client.get(results_url(igcse.exam, igcse.level, report=kind)).content.decode()
        assert "has no pass marks" in body


def test_the_card_shows_the_class_highest_mark_without_storing_it(erp, board):
    mark_everything(erp, board, scores(board, **{"101": (90, 70), "109": (60, 95)}))
    publish_exam(board.exam, erp.admin)
    body = login(erp.admin).get(f"/exams/{board.exam.pk}/report/{board.humanities.student_id}/").content.decode()
    assert "Highest</th>" in body
    assert '<td class="text-right text-slate-500">90</td>' in body  # Bangla 1st paper: Nabila's 90
    assert '<td class="text-right text-slate-500">95</td>' in body  # General Math: Rupa's own 95
    # Bangla graded on both papers: 90 + 85 beats 70 + 85.
    assert '<td class="text-right text-slate-500">175</td>' in body
    # Worked out when shown, never frozen into a published result.
    assert all("class_highest" not in s.payload for s in ResultSnapshot.objects.filter(exam=board.exam))


def test_the_printed_card_has_the_highest_column_too(erp, board):
    from examinations.documents import class_highest, with_class_highest

    mark_everything(erp, board, scores(board, **{"101": (90, 70)}))
    publish_exam(board.exam, erp.admin)
    rows = build_result_sheet(board.exam, board.level)["rows"]
    row = next(r for r in rows if r["enrollment_id"] == board.humanities.pk)
    snapshot = ResultSnapshot.objects.get(exam=board.exam, enrollment=board.humanities)
    printed = card_texts(
        report_card_flowables(
            erp.school, board.exam, board.humanities, with_class_highest(row, class_highest(rows)), snapshot, styles()
        )
    )
    assert "Highest" in printed and "90" in printed
    # Section cards print it as well.
    response = login(erp.admin).get(f"/exams/report-cards.pdf?exam={board.exam.pk}&section={board.section.pk}")
    assert response.status_code == 200


def test_cards_without_positions_show_no_highest_marks(erp, igcse):
    from decimal import Decimal

    from examinations.services import save_mark

    for pupil, score in zip(igcse.pupils, (92, 55), strict=True):
        save_mark(
            user=erp.admin,
            schedule=igcse.papers["0625"],
            enrollment=pupil,
            components={"mcq": "30", "theory": "60", "practical": "30"},
        )
        save_mark(user=erp.admin, schedule=igcse.papers["0510"], enrollment=pupil, score=Decimal(score))
    body = login(erp.admin).get(f"/exams/{igcse.exam.pk}/report/{igcse.pupils[1].student_id}/").content.decode()
    assert "Highest</th>" not in body
