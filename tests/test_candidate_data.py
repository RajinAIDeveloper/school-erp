"""
Candidate data for exam officers: the entries file with its checks, access arrangements kept
from everyone but managers, and the national board registration (eSIF) fields with their gaps.
"""

import csv
import io
from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client

from examinations.candidates import entry_problems, entry_rows, registration_rows, save_arrangement
from examinations.models import AccessArrangement, ExamSeries, SeriesCandidate
from examinations.official import add_candidates, add_entries, update_candidate
from users.models import User


def login(user):
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def series(erp):
    series = ExamSeries.objects.create(school=erp.school, body="cambridge", name="June 2027", centre_number="BD123")
    add_candidates(user=erp.admin, series=series, students=[erp.student])
    return series


def enter(erp, series):
    add_entries(
        user=erp.admin,
        series=series,
        candidates=list(series.candidates.all()),
        qualification="IGCSE",
        syllabus_code="0625",
        syllabus_title="Physics",
        tier="extended",
    )


# ============================================================ entries


def test_the_entries_file_carries_what_the_body_asks_for(erp, series):
    enter(erp, series)
    headers, rows = entry_rows(series)
    row = dict(zip(headers, rows[0], strict=True))
    assert row["Centre"] == "BD123" and row["Candidate number"] == "0001"
    assert row["Date of birth"] == "01/01/2016"
    assert row["Name on certificate"] == "Ayesha"
    assert (row["Syllabus code"], row["Tier or level"]) == ("0625", "Extended")
    candidate = SeriesCandidate.objects.get()
    update_candidate(user=erp.admin, candidate=candidate, candidate_number="0001", certificate_name="Ayesha Rahman")
    assert dict(zip(headers, entry_rows(series)[1][0], strict=True))["Name on certificate"] == "Ayesha Rahman"


def test_the_checks_name_what_to_fix_before_sending(erp, series):
    series.centre_number = ""
    series.save()
    candidate = SeriesCandidate.objects.get()
    candidate.candidate_number = "12"
    candidate.certificate_name = "x" * 60
    candidate.save()
    erp.student.first_name = "A" * 70
    erp.student.save()
    problems = " ".join(entry_problems(series))
    assert "no centre number" in problems
    assert "four digits" in problems
    assert "not entered for any syllabus" in problems
    candidate.certificate_name = ""
    candidate.save()
    assert "certificates allow 60" in " ".join(entry_problems(series))


def test_a_certificate_name_over_60_characters_is_refused(erp, series):
    with pytest.raises(ValidationError):
        update_candidate(
            user=erp.admin, candidate=SeriesCandidate.objects.get(), candidate_number="0001", certificate_name="y" * 61
        )


def test_the_entries_download(erp, series):
    enter(erp, series)
    response = login(erp.admin).get(f"/exams/series/{series.pk}/entries.csv")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert ["Centre", "BD123"] in rows
    assert any(r[:2] == ["BD123", "0001"] for r in rows)
    assert login(erp.admin).get(f"/exams/series/{series.pk}/entries.csv?format=xlsx").status_code == 200


# ============================================================ access arrangements


def test_an_arrangement_needs_consent_before_it_is_applied_for(erp, series):
    candidate = SeriesCandidate.objects.get()
    save_arrangement(user=erp.admin, candidate=candidate, kind="extra_time", details="25% extra time")
    with pytest.raises(ValidationError):
        save_arrangement(user=erp.admin, candidate=candidate, kind="reader", status="applied")
    save_arrangement(user=erp.admin, candidate=candidate, kind="reader", status="applied", consent_on=date(2026, 9, 1))
    assert AccessArrangement.objects.count() == 2


def test_only_managers_keep_or_see_arrangements(erp, series):
    candidate = SeriesCandidate.objects.get()
    with pytest.raises(PermissionDenied):
        save_arrangement(user=erp.teacher, candidate=candidate, kind="extra_time")
    save_arrangement(user=erp.admin, candidate=candidate, kind="extra_time", details="25% extra time")
    assert "25% extra time" in login(erp.admin).get(f"/exams/series/{series.pk}/").content.decode()
    # An office user who may keep series but is not a manager does not see them.
    clerk = User.objects.create_user(username="clerk", school=erp.school, password="Test-pass-9842")
    clerk.groups.add(Group.objects.get(name="Staff"))
    from django.contrib.auth.models import Permission

    clerk.user_permissions.add(Permission.objects.get(codename="view_examseries"))
    page = login(clerk).get(f"/exams/series/{series.pk}/").content.decode()
    assert "Access arrangements" not in page and "25% extra time" not in page


def test_the_series_page_records_an_arrangement(erp, series):
    candidate = SeriesCandidate.objects.get()
    response = login(erp.admin).post(
        f"/exams/series/{series.pk}/",
        {
            "action": "arrangement",
            "candidate": candidate.pk,
            "kind": "scribe",
            "status": "applied",
            "details": "Scribe for written papers",
            "consent_on": "2026-09-01",
        },
    )
    assert response.status_code == 302
    assert AccessArrangement.objects.get().get_kind_display() == "Scribe or word processor"


# ============================================================ national board registration


def test_registration_rows_list_each_students_own_subject_codes(erp, board):
    headers, rows, checks = registration_rows(erp.year, board.level)
    by_name = {row[headers.index("Name (English)")]: dict(zip(headers, row, strict=True)) for row in rows}
    science, humanities = by_name["Nabila"], by_name["Rupa"]
    assert science["Subject codes"].split() == ["101", "102", "109", "111", "136"]
    assert science["4th subject code"] == "126"
    assert humanities["Subject codes"].split() == ["101", "102", "109", "110", "112"]
    assert humanities["Group"] == "Humanities"
    joined = " ".join(checks)
    assert "Roll 1 Nabila: no Bangla name." in joined
    assert "birth registration number should be 17 digits" in joined


def test_a_complete_record_has_no_gaps_but_the_photo(erp, board):
    s = board.science.student
    s.name_bn, s.father_name, s.father_name_bn = "নাবিলা", "Karim", "করিম"
    s.mother_name, s.mother_name_bn = "Salma", "সালমা"
    s.birth_registration_no, s.father_nid = "2011" + "0" * 13, "1234567890"
    s.save()
    _headers, _rows, checks = registration_rows(erp.year, board.level)
    mine = [c for c in checks if "Nabila" in c]
    assert mine == ["Roll 1 Nabila: no photo (the board asks for 300 × 300 pixels)."]


def test_the_registration_page_is_for_managers_only(erp, board):
    admin = login(erp.admin)
    page = admin.get(f"/exams/registration/?class_level={board.level.pk}").content.decode()
    assert "gaps to fill" in page and "Nabila" in page
    download = admin.get(f"/exams/registration/?class_level={board.level.pk}&format=csv")
    assert "Birth registration no." in download.content.decode("utf-8-sig")
    assert login(erp.teacher).get("/exams/registration/").status_code == 403
