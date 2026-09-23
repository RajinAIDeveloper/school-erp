"""
Exam series, entries and official results: the exam officer's record, kept apart from the
school's own assessments. Results are matched by candidate number, amended without losing
the earlier record, and shown to families only once a second person has confirmed them.
"""

from datetime import date, timedelta

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from examinations.models import ExamSeries, OfficialResult, SeriesCandidate, SeriesEntry
from examinations.official import (
    add_candidates,
    add_entries,
    check_results,
    confirm_results,
    import_results,
    official_results_for,
    update_candidate,
    withdraw_entry,
)
from users.models import User


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def series(erp):
    return ExamSeries.objects.create(
        school=erp.school, body="cambridge", name="June 2027", centre_number="BD123", results_date=date(2027, 8, 12)
    )


@pytest.fixture
def principal(erp):
    user = User.objects.create_user(username="principal", school=erp.school, password="Test-pass-9842")
    user.groups.add(Group.objects.get(name="Principal"))
    return user


@pytest.fixture
def entered(erp, series):
    add_candidates(user=erp.admin, series=series, students=[erp.student])
    candidate = SeriesCandidate.objects.get()
    add_entries(
        user=erp.admin,
        series=series,
        candidates=[candidate],
        qualification="IGCSE",
        syllabus_code="0625",
        syllabus_title="Physics",
        tier="extended",
    )
    return candidate


def rows(*lines):
    return [
        {"candidate_number": n, "syllabus_code": c, "grade": g, "syllabus_title": "", "points": p, "kind": ""}
        for n, c, g, p in lines
    ]


def record(erp, series, lines, reason="", user=None):
    prepared, errors = check_results(series, rows(*lines))
    assert not errors, errors
    return import_results(
        user=user or erp.admin,
        series=series,
        prepared=prepared,
        source="Cambridge statement of results",
        received_on=date(2026, 9, 1),
        amendment_reason=reason,
    )


# ============================================================ candidates and entries


def test_candidates_are_numbered_once_and_numbers_stay_unique(erp, series):
    assert add_candidates(user=erp.admin, series=series, students=[erp.student], first_number=101) == 1
    assert add_candidates(user=erp.admin, series=series, students=[erp.student]) == 0
    candidate = SeriesCandidate.objects.get()
    assert candidate.candidate_number == "0101"
    from datetime import date as d

    from students.models import Student

    other = Student.objects.create(
        school=erp.school,
        student_id="S2",
        first_name="Rafi",
        gender="M",
        date_of_birth=d(2016, 1, 1),
        admission_date=d(2026, 1, 1),
    )
    add_candidates(user=erp.admin, series=series, students=[other])
    second = SeriesCandidate.objects.get(student=other)
    assert second.candidate_number == "0102"
    with pytest.raises(ValidationError):
        update_candidate(user=erp.admin, candidate=second, candidate_number="0101")
    update_candidate(user=erp.admin, candidate=second, candidate_number="0250", uci="BD123X0250")
    second.refresh_from_db()
    assert (second.candidate_number, second.uci) == ("0250", "BD123X0250")


def test_entries_are_not_duplicated_and_withdrawals_are_kept(erp, series, entered):
    made, skipped = add_entries(
        user=erp.admin,
        series=series,
        candidates=[entered],
        qualification="IGCSE",
        syllabus_code="0625",
        syllabus_title="Physics",
    )
    assert (made, skipped) == (0, 1)
    entry = SeriesEntry.objects.get()
    withdraw_entry(user=erp.admin, entry=entry)
    entry.refresh_from_db()
    assert entry.status == "withdrawn" and entry.withdrawn_at
    with pytest.raises(ValidationError):
        withdraw_entry(user=erp.admin, entry=entry)
    with pytest.raises(ValidationError):
        add_entries(
            user=erp.admin,
            series=series,
            candidates=[entered],
            qualification="",
            syllabus_code="0620",
            syllabus_title="Chem",
        )


def test_a_candidate_from_another_series_cannot_be_entered(erp, series, entered):
    other = ExamSeries.objects.create(school=erp.school, body="pearson", name="May 2027")
    with pytest.raises(ValidationError):
        add_entries(
            user=erp.admin,
            series=other,
            candidates=[entered],
            qualification="IAL",
            syllabus_code="WMA11",
            syllabus_title="P1",
        )


# ============================================================ importing official results


def test_rows_are_matched_by_candidate_number_and_problems_named(erp, series, entered):
    prepared, errors = check_results(
        series,
        rows(("0001", "0625", "A*", ""), ("9999", "0625", "A", ""), ("0001", "0625", "B", ""), ("0001", "", "C", "")),
    )
    assert [r["action"] for r in prepared] == ["new"]
    assert prepared[0]["student"] == "Ayesha" and prepared[0]["entered"]
    assert any("no candidate 9999" in e for e in errors)
    assert any("appears twice" in e for e in errors)
    assert any("syllabus code and a grade" in e for e in errors)


def test_an_import_needs_a_source_and_a_real_date(erp, series, entered):
    prepared, _ = check_results(series, rows(("0001", "0625", "A", "")))
    with pytest.raises(ValidationError):
        import_results(user=erp.admin, series=series, prepared=prepared, source="", received_on=date(2026, 9, 1))
    with pytest.raises(ValidationError):
        import_results(
            user=erp.admin, series=series, prepared=prepared, source="SoR", received_on=date.today() + timedelta(days=1)
        )
    assert not OfficialResult.objects.exists()


def test_a_second_person_confirms_before_families_see_a_result(erp, series, entered, principal):
    assert record(erp, series, [("0001", "0625", "A", "")]) == (1, 0)
    assert official_results_for(erp.student, confirmed_only=True) == []
    # The importer cannot confirm their own import.
    assert confirm_results(user=erp.admin, series=series) == (0, 1)
    with pytest.raises(PermissionDenied):
        confirm_results(user=erp.teacher, series=series)
    assert confirm_results(user=principal, series=series) == (1, 0)
    shown = official_results_for(erp.student, confirmed_only=True)
    assert [(r.syllabus_code, r.grade, r.checked_by) for r in shown] == [("0625", "A", principal)]
    portal = login(erp.parent).get(f"/portal/results/?student={erp.student.pk}").content.decode()
    assert "Official results" in portal and "Cambridge statement of results" in portal


def test_an_amendment_keeps_the_earlier_result_and_needs_a_reason(erp, series, entered, principal):
    record(erp, series, [("0001", "0625", "B", "")])
    confirm_results(user=principal, series=series)
    prepared, _ = check_results(series, rows(("0001", "0625", "A", "")))
    assert prepared[0]["action"] == "amend" and prepared[0]["previous"] == "B"
    with pytest.raises(ValidationError):
        import_results(user=erp.admin, series=series, prepared=prepared, source="SoR", received_on=date(2026, 9, 5))
    record(erp, series, [("0001", "0625", "A", "")], reason="Enquiry about results upheld")
    old, new = OfficialResult.objects.order_by("created_at")
    assert not old.is_current and new.is_current and new.supersedes == old
    assert new.amendment_reason == "Enquiry about results upheld"
    # The amended result waits for confirmation; until then the family sees none for it.
    assert official_results_for(erp.student, confirmed_only=True) == []
    confirm_results(user=principal, series=series)
    staff_page = login(erp.admin).get(f"/students/{erp.student.pk}/").content.decode()
    assert "Amended from B" in staff_page


def test_repeating_a_result_changes_nothing(erp, series, entered):
    record(erp, series, [("0001", "0625", "A", "")])
    assert record(erp, series, [("0001", "0625", "A", "")]) == (0, 0)
    assert OfficialResult.objects.count() == 1


def test_official_results_never_appear_on_the_schools_report_card(erp, series, entered, principal):
    from decimal import Decimal

    from examinations.services import save_mark

    record(erp, series, [("0001", "0625", "A*", "")])
    confirm_results(user=principal, series=series)
    save_mark(user=erp.teacher, schedule=erp.schedule, enrollment=erp.enrollment, score=Decimal(80))
    card = login(erp.admin).get(f"/exams/{erp.exam.pk}/report/{erp.student.pk}/").content.decode()
    assert "0625" not in card and "A*" not in card


# ============================================================ screens and access


def test_the_results_screen_checks_then_records(erp, series, entered):
    client = login(erp.admin)
    url = f"/exams/series/{series.pk}/results/"
    upload = SimpleUploadedFile("sor.csv", b"candidate_number,syllabus_code,grade,points\n0001,0625,A,88\n")
    preview = client.post(url, {"action": "check", "file": upload})
    assert preview.status_code == 200 and b"What this file will do" in preview.content
    assert not OfficialResult.objects.exists()
    done = client.post(url, {"action": "import", "source": "Statement of results", "received_on": "2026-09-01"})
    assert done.status_code == 302
    result = OfficialResult.objects.get()
    assert (result.grade, result.points, result.checked_at) == ("A", "88", None)


def test_the_series_screens_register_and_enter(erp, series):
    client = login(erp.admin)
    url = f"/exams/series/{series.pk}/"
    assert client.get("/exams/series/").status_code == 200
    assert client.get("/exams/series/manage/new/").status_code == 200
    client.post(url, {"action": "add_candidates", "section": erp.section.pk, "first_number": "1"})
    candidate = SeriesCandidate.objects.get()
    client.post(
        url,
        {
            "action": "add_entries",
            "candidates": [candidate.pk],
            "qualification": "IGCSE",
            "syllabus_code": "0625",
            "syllabus_title": "Physics",
            "tier": "core",
        },
    )
    assert SeriesEntry.objects.get().tier == "core"
    assert b"0625" in client.get(url).content


def test_teachers_and_families_cannot_open_series(erp, series):
    for user in (erp.teacher, erp.parent):
        client = login(user)
        assert client.get("/exams/series/").status_code == 403
        assert client.get(f"/exams/series/{series.pk}/results/").status_code == 403


def test_another_schools_series_is_not_found(erp):
    theirs = ExamSeries.objects.create(school=erp.other, body="ib", name="May 2027")
    client = login(erp.admin)
    assert client.get(f"/exams/series/{theirs.pk}/").status_code == 404
    assert client.get(f"/exams/series/{theirs.pk}/results/").status_code == 404
