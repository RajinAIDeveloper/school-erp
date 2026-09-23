"""
Board-rule results: the national curriculum's GPA, groups, 4th subject, combined papers
and parts, applied only where a class follows the national curriculum.

The worked examples at the top check the arithmetic on its own. The rest check it end to end,
from marks entered on the grid to a published, frozen result.

These rules follow the education boards' published practice as researched in September 2026.
Two details — the exact rounding of GPA and the pass rule for combined papers — should be
confirmed against a school's own board results before the system is relied on for them.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group as AuthGroup
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from academics.models import ClassLevel, ClassSubject, Section, Subject, SubjectTeacher
from core.models import AssessmentSystem
from examinations.grading import (
    board_pass_mark,
    combine_units,
    grade_paper,
    national_outcome,
    standard_outcome,
)
from examinations.models import Exam, ExamSchedule, Mark, PaperComponent, ResultSnapshot, UnlockRequest
from examinations.services import (
    build_result_sheet,
    publish_exam,
    review_unlock,
    save_mark,
    save_marks,
)
from messaging.models import SMSMessage
from students.models import Enrollment, Guardian, Student, StudentGuardian
from users.models import User

BD_RULES = [
    {"letter": "A+", "min_percent": "80", "max_percent": "100", "grade_point": "5.00"},
    {"letter": "A", "min_percent": "70", "max_percent": "79.99", "grade_point": "4.00"},
    {"letter": "A-", "min_percent": "60", "max_percent": "69.99", "grade_point": "3.50"},
    {"letter": "B", "min_percent": "50", "max_percent": "59.99", "grade_point": "3.00"},
    {"letter": "C", "min_percent": "40", "max_percent": "49.99", "grade_point": "2.00"},
    {"letter": "D", "min_percent": "33", "max_percent": "39.99", "grade_point": "1.00"},
    {"letter": "F", "min_percent": "0", "max_percent": "32.99", "grade_point": "0.00"},
]


# ============================================================ the arithmetic, on its own


def unit(name, gp, *, passed=True, fourth=False, missing=False):
    return {
        "name": name,
        "grade_point": str(gp),
        "passed": passed,
        "is_fourth": fourth,
        "missing": missing,
        "absent": False,
    }


def test_the_fourth_subject_adds_only_what_it_scores_above_two():
    # Five main subjects: 5 + 4 + 4 + 3.5 + 5 = 21.5. A 4th subject at 4.00 adds 2.00.
    units = [unit("B", 5), unit("E", 4), unit("M", 4), unit("S", "3.5"), unit("R", 5), unit("HM", 4, fourth=True)]
    outcome = national_outcome(units, BD_RULES)
    assert outcome["result"] == "PASS"
    assert outcome["gpa"] == Decimal("4.70")  # (21.5 + 2) / 5
    assert outcome["gpa_without_fourth"] == Decimal("4.30")  # 21.5 / 5
    assert outcome["gpa_letter"] == "A"


def test_the_fourth_subject_is_left_out_of_the_divisor():
    units = [unit("B", 3), unit("E", 3), unit("HM", "3.5", fourth=True)]
    # Averaged over three subjects this would be 3.17; the rule gives (6 + 1.5) / 2.
    assert national_outcome(units, BD_RULES)["gpa"] == Decimal("3.75")


def test_a_fourth_subject_at_or_below_two_adds_nothing():
    for fourth_gp in ("2.00", "1.00", "0.00"):
        units = [unit("B", 4), unit("E", 4), unit("HM", fourth_gp, fourth=True)]
        assert national_outcome(units, BD_RULES)["gpa"] == Decimal("4.00")


def test_a_failed_fourth_subject_never_fails_the_student():
    units = [unit("B", 4), unit("E", 4), unit("HM", 0, passed=False, fourth=True)]
    outcome = national_outcome(units, BD_RULES)
    assert outcome["result"] == "PASS"
    assert outcome["gpa"] == Decimal("4.00")


def test_one_failed_main_subject_fails_the_result_and_zeroes_the_gpa():
    units = [unit("B", 5), unit("E", 0, passed=False), unit("HM", 5, fourth=True)]
    outcome = national_outcome(units, BD_RULES)
    assert outcome["result"] == "FAIL"
    assert outcome["gpa"] == Decimal("0.00")


def test_gpa_never_exceeds_five():
    units = [unit("B", 5), unit("E", 5), unit("HM", 5, fourth=True)]
    assert national_outcome(units, BD_RULES)["gpa"] == Decimal("5.00")


def test_a_missing_paper_leaves_the_result_incomplete():
    units = [unit("B", 5), unit("E", 0, missing=True)]
    assert national_outcome(units, BD_RULES)["result"] == "INCOMPLETE"


def test_other_schools_count_every_paper_the_same():
    """An English-medium school's optional paper is an ordinary paper: it counts and it can fail."""
    units = [unit("B", 4), unit("E", 4), unit("X", 0, passed=False, fourth=True)]
    outcome = standard_outcome(units, BD_RULES)
    assert outcome["result"] == "FAIL"


@pytest.mark.parametrize("full,expected", [(70, 23), (30, 10), (50, 17), (25, 8), (100, 33)])
def test_the_pass_mark_of_a_part_is_33_percent_as_the_boards_print_it(full, expected):
    assert board_pass_mark(full) == Decimal(expected)


def paper(schedule_id, unit_id, name, full=100, pass_marks=33, components=(), role="main"):
    return {
        "schedule_id": schedule_id,
        "subject": name,
        "subject_id": schedule_id,
        "subject_code": "",
        "unit_id": unit_id,
        "unit_name": "Bangla" if unit_id == 1 else name,
        "full_marks": Decimal(full),
        "pass_marks": Decimal(pass_marks),
        "components": list(components),
        "role": role,
    }


def test_a_paper_fails_when_one_part_is_below_its_pass_mark():
    """70 of 100 overall is an A, but 9 of 30 in MCQ is below the part's pass mark of 10."""
    spec = paper(1, 9, "Physics", components=[("cq", "Creative", 70, 23), ("mcq", "MCQ", 30, 10)])
    cell = grade_paper(spec, {"absent": False, "score": Decimal(70), "parts": {"cq": "61", "mcq": "9"}}, BD_RULES)
    assert not cell["passed"] and cell["failed_part"]
    assert cell["letter"] == "F"


def test_two_papers_of_one_subject_are_graded_on_their_combined_marks():
    """Bangla 1st paper 90, 2nd paper 60: 150 of 200 is 75%, an A, graded once."""
    first = grade_paper(paper(1, 1, "Bangla 1st paper"), {"absent": False, "score": Decimal(90), "parts": {}}, BD_RULES)
    second = grade_paper(
        paper(2, 1, "Bangla 2nd paper"), {"absent": False, "score": Decimal(60), "parts": {}}, BD_RULES
    )
    units = combine_units([first, second], BD_RULES, combine=True)
    assert len(units) == 1
    assert units[0]["name"] == "Bangla"
    assert units[0]["score"] == "150" and units[0]["full_marks"] == "200"
    assert units[0]["letter"] == "A" and units[0]["grade_point"] == "4.00"


def test_a_weak_paper_can_be_carried_by_its_partner():
    """30 in one paper would fail it alone; 30 + 60 = 90 of 200 passes the combined 66."""
    first = grade_paper(paper(1, 1, "Bangla 1st paper"), {"absent": False, "score": Decimal(30), "parts": {}}, BD_RULES)
    second = grade_paper(
        paper(2, 1, "Bangla 2nd paper"), {"absent": False, "score": Decimal(60), "parts": {}}, BD_RULES
    )
    combined = combine_units([first, second], BD_RULES, combine=True)[0]
    assert not first["passed"]
    assert combined["passed"]


def test_without_board_rules_every_paper_stands_alone():
    first = grade_paper(paper(1, 1, "Bangla 1st paper"), {"absent": False, "score": Decimal(90), "parts": {}}, BD_RULES)
    second = grade_paper(
        paper(2, 1, "Bangla 2nd paper"), {"absent": False, "score": Decimal(60), "parts": {}}, BD_RULES
    )
    assert len(combine_units([first, second], BD_RULES, combine=False)) == 2


# ============================================================ end to end


def mark(erp, schedule, enrollment, score, absent=False):
    return save_mark(user=erp.admin, schedule=schedule, enrollment=enrollment, score=Decimal(score), absent=absent)


def mark_everything(erp, board, scores=None):
    scores = scores or {}
    for code, schedule in board.schedules.items():
        for enrollment in (board.science, board.humanities):
            from examinations.subjects import takes_paper

            if takes_paper(enrollment, schedule):
                mark(erp, schedule, enrollment, scores.get((enrollment.pk, code), 85))


def test_each_student_sits_only_their_own_papers(erp, board):
    """A Humanities student never sits Physics, and a Hindu student never sits Islam."""
    from examinations.subjects import takes_paper

    s = board.schedules
    assert takes_paper(board.science, s["136"]) and not takes_paper(board.humanities, s["136"])
    assert takes_paper(board.humanities, s["110"]) and not takes_paper(board.science, s["110"])
    assert takes_paper(board.science, s["111"]) and not takes_paper(board.science, s["112"])
    assert takes_paper(board.humanities, s["112"]) and not takes_paper(board.humanities, s["111"])
    assert takes_paper(board.science, s["126"]) and not takes_paper(board.science, s["134"])


def test_a_class_with_different_groups_can_now_be_published(erp, board):
    """Publication used to demand a Physics mark from the Humanities student, so it never could."""
    mark_everything(erp, board)
    publish_exam(board.exam, erp.admin)
    assert ResultSnapshot.objects.filter(exam=board.exam).count() == 2


def test_marking_a_paper_the_student_does_not_sit_is_refused(erp, board):
    with pytest.raises(ValidationError, match="does not sit"):
        mark(erp, board.schedules["136"], board.humanities, 70)


def test_the_published_result_applies_the_fourth_subject_and_combines_bangla(erp, board):
    s = board.schedules
    sci = board.science.pk
    mark_everything(
        erp,
        board,
        {
            (sci, "101"): 90,  # Bangla 150/200 = 75% -> A (4.00)
            (sci, "102"): 60,
            (sci, "109"): 85,  # A+ (5.00)
            (sci, "111"): 72,  # A (4.00)
            (sci, "136"): 65,  # A- (3.50)
            (sci, "126"): 80,  # 4th subject A+ (5.00) -> adds 3.00
        },
    )
    publish_exam(board.exam, erp.admin)
    row = ResultSnapshot.objects.get(enrollment=board.science).payload
    names = [u["name"] for u in row["subjects"]]
    assert names.count("Bangla") == 1 and "Bangla 1st paper" not in names
    # Main subjects: Bangla 4.00, Math 5.00, Islam 4.00, Physics 3.50 = 16.50 over 4.
    assert row["gpa_without_fourth"] == "4.13"
    assert row["gpa"] == "4.88"  # (16.50 + 3.00) / 4 = 4.875
    assert row["fourth_subject"] == "Higher Math"
    assert row["result"] == "PASS"
    assert s["126"].pk in [c["schedule_id"] for c in row["cells"] if c["is_fourth"]]


def test_failing_the_fourth_subject_does_not_fail_the_student(erp, board):
    mark_everything(erp, board, {(board.science.pk, "126"): 10})
    publish_exam(board.exam, erp.admin)
    row = ResultSnapshot.objects.get(enrollment=board.science).payload
    assert row["result"] == "PASS"


def test_a_part_below_its_pass_mark_fails_the_paper(erp, board):
    physics = board.schedules["136"]
    PaperComponent.objects.create(
        school=erp.school, schedule=physics, code="cq", name="Creative", full_marks=70, pass_marks=23
    )
    PaperComponent.objects.create(
        school=erp.school, schedule=physics, code="mcq", name="MCQ", full_marks=30, pass_marks=10
    )
    save_mark(user=erp.admin, schedule=physics, enrollment=board.science, components={"cq": "61", "mcq": "9"})
    stored = Mark.objects.get(schedule=physics, enrollment=board.science)
    assert stored.marks_obtained == Decimal("70.00")
    rows = build_result_sheet(board.exam, board.level)["rows"]
    cell = next(
        c for r in rows if r["enrollment_id"] == board.science.pk for c in r["cells"] if c["schedule_id"] == physics.pk
    )
    assert not cell["passed"] and cell["letter"] == "F"


def test_parts_must_all_be_entered_and_in_range(erp, board):
    physics = board.schedules["136"]
    PaperComponent.objects.create(
        school=erp.school, schedule=physics, code="cq", name="Creative", full_marks=70, pass_marks=23
    )
    PaperComponent.objects.create(
        school=erp.school, schedule=physics, code="mcq", name="MCQ", full_marks=30, pass_marks=10
    )
    with pytest.raises(ValidationError):
        save_mark(user=erp.admin, schedule=physics, enrollment=board.science, components={"cq": "61"})
    with pytest.raises(ValidationError):
        save_mark(user=erp.admin, schedule=physics, enrollment=board.science, components={"cq": "71", "mcq": "9"})


def test_the_grid_enters_marks_by_part(erp, board):
    physics = board.schedules["136"]
    PaperComponent.objects.create(
        school=erp.school, schedule=physics, code="cq", name="Creative", full_marks=70, pass_marks=23
    )
    PaperComponent.objects.create(
        school=erp.school, schedule=physics, code="mcq", name="MCQ", full_marks=30, pass_marks=10
    )
    client = Client()
    client.force_login(erp.admin)
    page = client.get(f"/exams/marks/?schedule={physics.pk}&section={board.section.pk}").content.decode()
    assert "Creative" in page and "MCQ" in page
    assert "Rupa" not in page  # the Humanities student does not sit Physics
    response = client.post(
        "/exams/marks/",
        {
            "schedule": physics.pk,
            "section": board.section.pk,
            f"{board.science.pk}-cq": "50",
            f"{board.science.pk}-mcq": "25",
            f"{board.science.pk}-version": "0",
        },
    )
    assert response.status_code == 302
    assert Mark.objects.get(schedule=physics, enrollment=board.science).marks_obtained == Decimal("75.00")


def test_an_english_medium_class_keeps_every_paper_on_its_own(erp, board):
    """The switch is per class: the same subjects under the other rulebook are not combined."""
    board.level.assessment_system = AssessmentSystem.OWN
    board.level.save()
    mark_everything(erp, board)
    rows = build_result_sheet(board.exam, board.level)["rows"]
    row = next(r for r in rows if r["enrollment_id"] == board.science.pk)
    assert "Bangla" not in [u["name"] for u in row["subjects"]]
    assert not any(c["is_fourth"] for c in row["cells"])


def test_an_internal_exam_can_opt_out_of_board_rules(erp, board):
    board.exam.assessment_system = AssessmentSystem.OWN
    board.exam.save()
    mark_everything(erp, board)
    row = next(r for r in build_result_sheet(board.exam, board.level)["rows"] if r["enrollment_id"] == board.science.pk)
    assert row["system"] == AssessmentSystem.OWN


def test_the_school_switch_decides_when_a_class_does_not(erp, board):
    board.level.assessment_system = ""
    board.level.save()
    board.level.refresh_from_db()
    assert not board.level.uses_board_rules  # the default is the school's own rules
    erp.school.assessment_system = AssessmentSystem.NATIONAL
    erp.school.save()
    board.level.refresh_from_db()
    assert board.level.uses_board_rules


def test_existing_classes_keep_their_old_behaviour_by_default(erp):
    """A school that never chooses a rulebook sees exactly the results it always did."""
    assert erp.school.assessment_system == AssessmentSystem.OWN
    assert erp.level.rules == AssessmentSystem.OWN


def test_merit_puts_every_pass_ahead_of_every_fail(erp, board):
    """A fail with high marks must not outrank a narrow pass."""
    hum = board.humanities.pk
    sci = board.science.pk
    mark_everything(
        erp,
        board,
        {(sci, code): 99 for code in board.schedules}
        | {(sci, "109"): 20}  # top marks, but fails Math
        | {(hum, code): 40 for code in board.schedules},
    )
    publish_exam(board.exam, erp.admin)
    fail = ResultSnapshot.objects.get(enrollment=board.science).payload
    passed = ResultSnapshot.objects.get(enrollment=board.humanities).payload
    assert fail["result"] == "FAIL" and passed["result"] == "PASS"
    assert passed["rank"] == 1 and fail["rank"] == 2


# ============================================================ the review's four scenarios


def test_a_teacher_cannot_even_see_marks_for_a_subject_they_do_not_teach_in_that_section(erp):
    """
    Math in section A and Science in section B gives no sight of section B's Math marks.

    The grid used to check only that the teacher taught Math somewhere in the class.
    """
    science = Subject.objects.create(school=erp.school, name="Science", code="SCI")
    SubjectTeacher.objects.create(
        school=erp.school, teacher=erp.employee, section=erp.other_section, subject=science, academic_year=erp.year
    )
    outsider = Student.objects.create(
        school=erp.school,
        student_id="B-1",
        first_name="Section B pupil",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    Enrollment.objects.create(
        school=erp.school,
        student=outsider,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.other_section,
        roll_number=1,
    )
    client = Client()
    client.force_login(erp.teacher)
    response = client.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.other_section.pk}")
    assert response.status_code == 403
    assert b"Section B pupil" not in response.content
    # Their own section still opens.
    assert client.get(f"/exams/marks/?schedule={erp.schedule.pk}&section={erp.section.pk}").status_code == 200


def test_clearing_an_absence_takes_the_mark_back_to_not_entered(erp):
    """'Clear all absences' used to leave the absence in place, because a blank row was skipped."""
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, absent=True)
    stored = Mark.objects.get()
    client = Client()
    client.force_login(erp.teacher)
    response = client.post(
        "/exams/marks/",
        {
            "schedule": erp.schedule.pk,
            "section": erp.section.pk,
            f"{erp.enrollment.pk}-score": "",
            f"{erp.enrollment.pk}-version": str(stored.version),
        },
    )
    assert response.status_code == 302
    assert not Mark.objects.exists()


def test_a_published_mark_cannot_be_cleared_only_corrected(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    publish_exam(erp.exam, erp.admin)
    unlock = UnlockRequest.objects.create(
        school=erp.school, schedule=erp.schedule, requested_by=erp.teacher, reason="Re-totalled"
    )
    review_unlock(unlock, erp.admin, True)
    from examinations.services import MarkEntryError

    with pytest.raises(MarkEntryError):
        save_marks(
            user=erp.teacher,
            schedule=erp.schedule,
            section=erp.section,
            rows=[(erp.enrollment, None, False, 1)],
        )
    assert Mark.objects.exists()


def test_a_one_cell_correction_makes_one_new_version_and_texts_only_that_family(
    erp, django_capture_on_commit_callbacks
):
    """
    Saving a corrected grid used to snapshot once per filled row, and text every family each
    time: thirty rows, thirty versions, thirty rounds of SMS for one fixed mark.
    """
    erp.school.notify_results_sms = True
    erp.school.save()
    others = []
    for roll in (2, 3, 4):
        pupil = Student.objects.create(
            school=erp.school,
            student_id=f"G{roll}",
            first_name=f"Pupil {roll}",
            gender="M",
            date_of_birth=date(2016, 1, 1),
            admission_date=date(2026, 1, 1),
        )
        enrollment = Enrollment.objects.create(
            school=erp.school,
            student=pupil,
            academic_year=erp.year,
            class_level=erp.level,
            section=erp.section,
            roll_number=roll,
        )
        guardian = Guardian.objects.create(school=erp.school, full_name=f"Parent {roll}", phone=f"0171234560{roll}")
        StudentGuardian.objects.create(student=pupil, guardian=guardian, relation="father", is_primary=True)
        others.append(enrollment)
    everyone = [erp.enrollment, *others]
    for enrollment in everyone:
        save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=enrollment, score=Decimal(60))
    with django_capture_on_commit_callbacks(execute=True):
        publish_exam(erp.exam, erp.admin)
    assert SMSMessage.objects.count() == 4

    unlock = UnlockRequest.objects.create(
        school=erp.school, schedule=erp.schedule, requested_by=erp.teacher, reason="One script re-totalled"
    )
    review_unlock(unlock, erp.admin, True)
    rows = [(enrollment, Decimal(60), False, 1) for enrollment in everyone]
    rows[0] = (erp.enrollment, Decimal(75), False, 1)  # the one real correction
    with django_capture_on_commit_callbacks(execute=True):
        saved = save_marks(user=erp.teacher, schedule=erp.schedule, section=erp.section, rows=rows)
    assert len(saved) == 1
    exam = Exam.objects.get(pk=erp.exam.pk)
    assert exam.publication_version == 2
    assert ResultSnapshot.objects.filter(exam=exam).values("version").distinct().count() == 2
    # Only Ayesha's family hears about the correction.
    corrections = SMSMessage.objects.filter(dedupe_key__endswith=":2")
    assert corrections.count() == 1
    assert "Ayesha" in corrections.get().body


def test_two_prints_of_one_published_version_are_identical(erp):
    """
    The card used to read attendance live, and over the whole year, so it changed after
    publication. Now everything it shows is frozen in the snapshot.
    """
    from attendance.models import StudentAttendance

    erp.exam.end_date = date(2026, 9, 25)
    erp.exam.save()
    StudentAttendance.objects.create(
        school=erp.school, enrollment=erp.enrollment, date=date(2026, 9, 21), status="present"
    )
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    client = Client()
    client.force_login(erp.admin)
    first = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").content

    # Things change after publication: more attendance, a renamed student.
    StudentAttendance.objects.create(
        school=erp.school, enrollment=erp.enrollment, date=date(2026, 9, 28), status="absent"
    )
    erp.student.first_name = "Renamed"
    erp.student.save()
    second = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").content

    def card(page):
        # The card itself; the page around it carries a fresh CSRF token on every render.
        return page[page.index(b"<article") : page.index(b"</article>")]

    assert card(first) == card(second)
    assert b"Renamed" not in second
    snapshot = ResultSnapshot.objects.get()
    assert snapshot.payload["attendance"] == {"total": 1, "present": 1, "percent": 100}


def test_a_card_is_judged_by_the_year_it_belongs_to_like_bulk_printing(erp):
    """
    One rule for historical cards: a teacher may open the card for a section they taught
    in the exam's year, whatever they teach now.
    """
    from academics.models import AcademicYear

    last_year = AcademicYear.objects.create(
        school=erp.school, name="2025", start_date=date(2025, 1, 1), end_date=date(2025, 12, 31)
    )
    old_enrollment = Enrollment.objects.create(
        school=erp.school,
        student=erp.student,
        academic_year=last_year,
        class_level=erp.level,
        section=erp.other_section,
        roll_number=9,
    )
    old_exam = Exam.objects.create(
        school=erp.school, academic_year=last_year, name="Annual 2025", grade_scale=erp.scale
    )
    old_schedule = ExamSchedule.objects.create(
        school=erp.school, exam=old_exam, class_level=erp.level, subject=erp.subject, full_marks=100, pass_marks=33
    )
    SubjectTeacher.objects.create(
        school=erp.school, teacher=erp.employee, section=erp.other_section, subject=erp.subject, academic_year=last_year
    )
    save_mark(user=erp.admin, schedule=old_schedule, enrollment=old_enrollment, score=Decimal(70))
    publish_exam(old_exam, erp.admin)

    client = Client()
    client.force_login(erp.teacher)
    assert client.get(f"/exams/{old_exam.pk}/report/{erp.student.pk}/").status_code == 200

    other_teacher_user = User.objects.create_user("t2", school=erp.school, password="Test-pass-9842")
    other_teacher_user.groups.add(AuthGroup.objects.get(name="Teacher"))
    from employees.models import Employee

    Employee.objects.create(
        school=erp.school,
        user=other_teacher_user,
        employee_id="T2",
        first_name="Other",
        gender="M",
        phone="01712345601",
        joining_date=date(2025, 1, 1),
    )
    client.force_login(other_teacher_user)
    assert client.get(f"/exams/{old_exam.pk}/report/{erp.student.pk}/").status_code == 404


def test_the_card_carries_a_qr_code_and_a_fingerprint(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    publish_exam(erp.exam, erp.admin)
    snapshot = ResultSnapshot.objects.get()
    client = Client()
    client.force_login(erp.admin)
    body = client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").content.decode()
    assert "<svg" in body and "Scan to verify" in body
    assert snapshot.payload["fingerprint"] in body
    assert f"/exams/verify/{snapshot.verification_code}/" in body
    assert client.get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/?format=pdf").content.startswith(b"%PDF")


def test_an_unchanged_submission_saves_nothing(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    assert (
        save_marks(
            user=erp.teacher,
            schedule=erp.schedule,
            section=erp.section,
            rows=[(erp.enrollment, Decimal("80.00"), False, 1)],
        )
        == []
    )
    assert Mark.objects.get().version == 1


def test_permission_is_still_denied_to_an_unassigned_teacher(erp):
    stranger = User.objects.create_user("t3", school=erp.school, password="Test-pass-9842")
    stranger.groups.add(AuthGroup.objects.get(name="Teacher"))
    with pytest.raises(PermissionDenied):
        save_mark(user=stranger, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(50))


def test_a_rejected_grid_shows_back_what_the_teacher_typed(erp):
    """A failed submission used to redraw the stored marks and lose the teacher's typing."""
    second = Student.objects.create(
        school=erp.school,
        student_id="TYPED-1",
        first_name="Second",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    other = Enrollment.objects.create(
        school=erp.school,
        student=second,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    client = Client()
    client.force_login(erp.teacher)
    response = client.post(
        "/exams/marks/",
        {
            "schedule": erp.schedule.pk,
            "section": erp.section.pk,
            f"{erp.enrollment.pk}-score": "77",
            f"{erp.enrollment.pk}-version": "0",
            f"{other.pk}-score": "150",  # over full marks: the whole grid is refused
            f"{other.pk}-version": "0",
        },
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert 'value="77"' in body and 'value="150"' in body
    assert not Mark.objects.exists()


def test_repeating_the_same_correction_makes_no_further_version(erp):
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    publish_exam(erp.exam, erp.admin)
    unlock = UnlockRequest.objects.create(
        school=erp.school, schedule=erp.schedule, requested_by=erp.teacher, reason="Fix"
    )
    review_unlock(unlock, erp.admin, True)
    save_marks(
        user=erp.teacher, schedule=erp.schedule, section=erp.section, rows=[(erp.enrollment, Decimal(72), False, 1)]
    )
    save_marks(
        user=erp.teacher, schedule=erp.schedule, section=erp.section, rows=[(erp.enrollment, Decimal(72), False, 2)]
    )
    assert Exam.objects.get(pk=erp.exam.pk).publication_version == 2


def test_a_stale_version_rejects_the_whole_batch(erp):
    second = Student.objects.create(
        school=erp.school,
        student_id="STALE-1",
        first_name="Second",
        gender="M",
        date_of_birth=date(2016, 1, 1),
        admission_date=date(2026, 1, 1),
    )
    other = Enrollment.objects.create(
        school=erp.school,
        student=second,
        academic_year=erp.year,
        class_level=erp.level,
        section=erp.section,
        roll_number=2,
    )
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(70))
    from examinations.services import MarkEntryError

    with pytest.raises(MarkEntryError):
        save_marks(
            user=erp.teacher,
            schedule=erp.schedule,
            section=erp.section,
            rows=[(other, Decimal(60), False, 0), (erp.enrollment, Decimal(80), False, 0)],  # stale: stored is 1
        )
    assert not Mark.objects.filter(enrollment=other).exists()
    assert Mark.objects.get(enrollment=erp.enrollment).marks_obtained == Decimal("70.00")
