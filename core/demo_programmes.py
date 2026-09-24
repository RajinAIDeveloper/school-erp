"""
Demonstration classes for the three programmes the product is sold for, so a school sees its
own kind of results the first time it signs in:

- Year 10 (Cambridge IGCSE): option subjects, a weighted Physics paper, comments, approved
  predicted grades, a published mock, and exam series with entries and an official result.
- DP1 (IB Diploma): six subjects at HL and SL with TOK, the Extended Essay and CAS.
- Class 9 (national curriculum): groups, religion papers and a 4th subject from the national
  plan, with a creative and multiple-choice paper.

Everything is created once; running the seed again changes nothing.
"""

from datetime import date
from decimal import Decimal

from django.utils import timezone

from academics.models import ClassLevel, ClassSubject, Section, Subject, SubjectTeacher
from core.models import AssessmentSystem
from examinations.models import Exam, ExamSchedule, ExamSeries, OfficialResult, SeriesCandidate
from examinations.presets import install_preset
from examinations.services import publish_exam, save_mark
from examinations.subjects import takes_paper
from students.models import Enrollment, Guardian, Student, StudentGuardian


def _student(school, year, section, student_id, first, last, gender, born, roll, **extra):
    student, _ = Student.objects.get_or_create(
        school=school,
        student_id=student_id,
        defaults={
            "first_name": first,
            "last_name": last,
            "gender": gender,
            "date_of_birth": born,
            "admission_date": date(2026, 1, 1),
            **extra,
        },
    )
    enrollment, _ = Enrollment.objects.get_or_create(
        school=school,
        student=student,
        academic_year=year,
        defaults={"section": section, "class_level": section.class_level, "roll_number": roll},
    )
    return student, enrollment


def _guardian(school, student, name, phone):
    guardian, _ = Guardian.objects.get_or_create(school=school, full_name=name, defaults={"phone": phone})
    StudentGuardian.objects.get_or_create(
        student=student, guardian=guardian, defaults={"relation": "mother", "is_primary": True}
    )


def _mark_all(admin, schedules, enrollments, score_for):
    for schedule in schedules:
        for enrollment in enrollments:
            if takes_paper(enrollment, schedule):
                parts = list(schedule.components.all())
                score = score_for(schedule, enrollment)
                if parts:
                    save_mark(
                        user=admin,
                        schedule=schedule,
                        enrollment=enrollment,
                        components={
                            p.code: str((p.full_marks * Decimal(score) / 100).quantize(Decimal("1"))) for p in parts
                        },
                    )
                else:
                    save_mark(
                        user=admin,
                        schedule=schedule,
                        enrollment=enrollment,
                        score=(schedule.full_marks * Decimal(score) / 100).quantize(Decimal("0.01")),
                    )


def seed_cambridge(school, year, admin, principal, teacher, demo_guardian):
    from examinations.feedback import record_forecasts, save_overall_comments, save_subject_comments
    from examinations.parts import apply_preset
    from students.privacy import record_consent

    scale, _ = install_preset(school, "cambridge-igcse")
    level, _ = ClassLevel.objects.get_or_create(
        school=school, name="Year 10 (IGCSE)", defaults={"order": 10, "assessment_system": AssessmentSystem.CAMBRIDGE}
    )
    section, _ = Section.objects.get_or_create(school=school, class_level=level, name="Blue")
    subjects = {}
    for code, name, kind in (
        # Named apart from the national curriculum's subjects, which are matched by name too.
        ("0510", "IGCSE English as a Second Language", "compulsory"),
        ("0580", "IGCSE Mathematics", "compulsory"),
        ("0625", "IGCSE Physics", "choice"),
        ("0620", "IGCSE Chemistry", "choice"),
        ("0450", "IGCSE Business Studies", "choice"),
    ):
        subject, _ = Subject.objects.get_or_create(school=school, code=code, defaults={"name": name})
        subject.class_levels.add(level)
        ClassSubject.objects.get_or_create(
            school=school, academic_year=year, class_level=level, subject=subject, group="", defaults={"kind": kind}
        )
        subjects[code] = subject
    SubjectTeacher.objects.get_or_create(
        school=school, academic_year=year, section=section, subject=subjects["0625"], defaults={"teacher": teacher}
    )
    pupils = [
        ("IG-001", "Zara", "Ahmed", "F", ["0625", "0620"], 92),
        ("IG-002", "Imran", "Hossain", "M", ["0625", "0450"], 74),
        ("IG-003", "Nusrat", "Jahan", "F", ["0620", "0450"], 63),
        ("IG-004", "Tahmid", "Karim", "M", ["0625", "0620"], 51),
    ]
    enrollments = []
    for roll, (sid, first, last, gender, options, _level) in enumerate(pupils, 1):
        student, enrollment = _student(school, year, section, sid, first, last, gender, date(2010, roll, 10), roll)
        enrollment.chosen_subjects.set([subjects[c] for c in options])
        enrollments.append(enrollment)
        _guardian(school, student, f"{last} family", f"0171100000{roll}")
        if roll <= 3:
            record_consent(user=admin, student=student, purpose="abroad", given=True)
    # The demo guardian also has a child here, to show a family across two classes.
    StudentGuardian.objects.get_or_create(
        student=enrollments[0].student, guardian=demo_guardian, defaults={"relation": "mother", "is_primary": False}
    )

    exam, created = Exam.objects.get_or_create(
        school=school,
        academic_year=year,
        name="IGCSE Mock",
        defaults={"grade_scale": scale, "start_date": date(2026, 9, 7), "end_date": date(2026, 9, 14)},
    )
    if exam.status == "published":
        return
    schedules = []
    for code, subject in subjects.items():
        schedule, _ = ExamSchedule.objects.get_or_create(
            school=school,
            exam=exam,
            class_level=level,
            subject=subject,
            defaults={"date": date(2026, 9, 7), "full_marks": 100, "pass_marks": 0},
        )
        schedules.append(schedule)
        if code == "0625" and not schedule.components.exists():
            apply_preset(user=admin, schedule=schedule, key="cambridge-science-extended")
    levels = {e.pk: pupils[i][5] for i, e in enumerate(enrollments)}
    _mark_all(admin, schedules, enrollments, lambda s, e: levels[e.pk] - (3 if s.subject.code == "0580" else 0))
    physics = next(s for s in schedules if s.subject.code == "0625")
    takers = [e for e in enrollments if takes_paper(e, physics)]
    save_subject_comments(
        user=admin,
        schedule=physics,
        section=section,
        rows=[(e, "A" if levels[e.pk] > 70 else "B", "Careful practical work; revise electricity.") for e in takers],
    )
    save_overall_comments(
        user=admin,
        exam=exam,
        section=section,
        rows=[(e, "", "A steady term. Keep up regular revision before the final exams.") for e in enrollments],
    )
    record_forecasts(
        user=admin,
        section=section,
        subject=subjects["0580"],
        academic_year=year,
        kind="predicted",
        as_of=date(2026, 9, 1),
        rows=[(e, g) for e, g in zip(enrollments, ["A*", "A", "B", "C"], strict=True)],
    )
    publish_exam(exam, admin)

    june, _ = ExamSeries.objects.get_or_create(
        school=school,
        body="cambridge",
        name="June 2027",
        defaults={"centre_number": "BD999", "entry_deadline": date(2027, 2, 21), "results_date": date(2027, 8, 12)},
    )
    from examinations.official import add_candidates, add_entries

    add_candidates(user=admin, series=june, students=[e.student for e in enrollments])
    for code, subject in subjects.items():
        takers = [
            c
            for c in june.candidates.select_related("student")
            if code
            in {s.code for s in Enrollment.objects.get(student=c.student, academic_year=year).chosen_subjects.all()}
            or code in ("0510", "0580")
        ]
        add_entries(
            user=admin,
            series=june,
            candidates=takers,
            qualification="IGCSE",
            syllabus_code=code,
            syllabus_title=subject.name,
            tier="extended" if code in ("0580", "0625", "0620") else "",
            subject=subject,
        )
    early, _ = ExamSeries.objects.get_or_create(
        school=school, body="cambridge", name="November 2025", defaults={"centre_number": "BD999"}
    )
    candidate, _ = SeriesCandidate.objects.get_or_create(
        school=school, series=early, student=enrollments[0].student, defaults={"candidate_number": "0001"}
    )
    OfficialResult.objects.get_or_create(
        school=school,
        candidate=candidate,
        syllabus_code="0510",
        is_current=True,
        defaults={
            "syllabus_title": "English as a Second Language (0510)",
            "grade": "A",
            "source": "Cambridge statement of results",
            "received_on": date(2026, 1, 15),
            "recorded_by": admin,
            "checked_by": principal,
            "checked_at": timezone.now(),
        },
    )


def seed_ib(school, year, admin):
    scale, _ = install_preset(school, "ib-1-7")
    core_scale, _ = install_preset(school, "ib-core")
    level, _ = ClassLevel.objects.get_or_create(
        school=school, name="DP1 (IB Diploma)", defaults={"order": 11, "assessment_system": AssessmentSystem.IB_DP}
    )
    section, _ = Section.objects.get_or_create(school=school, class_level=level, name="A")
    exam, _ = Exam.objects.get_or_create(
        school=school,
        academic_year=year,
        name="DP Mock",
        defaults={"grade_scale": scale, "start_date": date(2026, 9, 7), "end_date": date(2026, 9, 18)},
    )
    if exam.status == "published":
        return
    schedules = []
    for name, ib_level, core in (
        ("Biology HL", "HL", ""),
        ("Chemistry HL", "HL", ""),
        ("Mathematics AA HL", "HL", ""),
        ("English A SL", "SL", ""),
        ("History SL", "SL", ""),
        ("Spanish B SL", "SL", ""),
        ("Theory of Knowledge", "", "tok"),
        ("Extended Essay", "", "ee"),
        ("CAS", "", "cas"),
    ):
        subject, _ = Subject.objects.get_or_create(
            school=school, name=name, defaults={"ib_level": ib_level, "ib_core": core}
        )
        schedule, _ = ExamSchedule.objects.get_or_create(
            school=school,
            exam=exam,
            class_level=level,
            subject=subject,
            defaults={
                "full_marks": 1 if core == "cas" else 100,
                "pass_marks": 1 if core == "cas" else 0,
                "grade_scale": core_scale if core in ("tok", "ee") else None,
            },
        )
        schedules.append(schedule)
    pupils = [("DP-001", "Samira", "Chowdhury", "F", 72), ("DP-002", "Arif", "Rahman", "M", 48)]
    enrollments = []
    for roll, (sid, first, last, gender, level_score) in enumerate(pupils, 1):
        student, enrollment = _student(school, year, section, sid, first, last, gender, date(2009, 3, roll), roll)
        _guardian(school, student, f"{last} family (DP)", f"0171200000{roll}")
        enrollments.append((enrollment, level_score))
    for enrollment, level_score in enrollments:
        for schedule in schedules:
            if schedule.subject.ib_core == "cas":
                score = Decimal(1)
            else:
                score = Decimal(level_score + (8 if schedule.subject.ib_level == "HL" else 0))
            save_mark(user=admin, schedule=schedule, enrollment=enrollment, score=score)
    publish_exam(exam, admin)


NATIONAL_EXAMS = (
    # (name, start, end, pattern): two published exams, so progress over time has something to show.
    ("First Term (Class 9)", date(2026, 3, 1), date(2026, 3, 12), 7),
    ("Half Yearly (Class 9)", date(2026, 6, 1), date(2026, 6, 15), 5),
)


def _national_score(level, code, pattern):
    """A believable spread: each student stronger in some subjects than others, never below the pass mark."""
    number = int(code) if code.isdigit() else sum(map(ord, code))
    return max(40, min(97, level + (number * pattern) % 17 - 8))


def seed_national(school, year, admin):
    from academics.presets import load_national_plan
    from examinations.parts import apply_preset

    scale, _ = install_preset(school, "bd-national")
    level, _ = ClassLevel.objects.get_or_create(
        school=school, name="Class 9 (national)", defaults={"order": 9, "assessment_system": AssessmentSystem.NATIONAL}
    )
    section, _ = Section.objects.get_or_create(
        school=school, class_level=level, name="A", defaults={"shift": "morning", "version": "bangla"}
    )
    if not ClassSubject.objects.filter(academic_year=year, class_level=level).exists():
        load_national_plan(school=school, user=admin, academic_year=year, class_level=level)
    by_code = {
        row.subject.code: row.subject
        for row in ClassSubject.objects.filter(academic_year=year, class_level=level).select_related("subject")
    }
    pupils = [
        ("C9-001", "Nabila", "Islam", "F", "islam", "science", ["138"], "126", 84),
        ("C9-002", "Rupa", "Das", "F", "hinduism", "humanities", ["141"], "134", 67),
        ("C9-003", "Sakib", "Hasan", "M", "islam", "business", [], "134", 58),
    ]
    enrollments, levels = [], {}
    for roll, (sid, first, last, gender, religion, group, chosen, fourth, level_score) in enumerate(pupils, 1):
        student, enrollment = _student(
            school, year, section, sid, first, last, gender, date(2011, roll, 5), roll, religion=religion
        )
        if enrollment.group != group:
            enrollment.group = group
            enrollment.fourth_subject = by_code[fourth]
            enrollment.save(update_fields=["group", "fourth_subject"])
            enrollment.chosen_subjects.set([by_code[c] for c in chosen])
        _guardian(school, student, f"{last} family (Class 9)", f"0171300000{roll}")
        enrollments.append(enrollment)
        levels[enrollment.pk] = level_score
    subjects = {row.subject for row in ClassSubject.objects.filter(academic_year=year, class_level=level)}
    for name, start, end, pattern in NATIONAL_EXAMS:
        exam, _ = Exam.objects.get_or_create(
            school=school,
            academic_year=year,
            name=name,
            defaults={"grade_scale": scale, "start_date": start, "end_date": end},
        )
        if exam.status == "published":
            continue
        schedules = []
        for subject in subjects:
            schedule, _ = ExamSchedule.objects.get_or_create(
                school=school,
                exam=exam,
                class_level=level,
                subject=subject,
                defaults={"full_marks": 100, "pass_marks": 33},
            )
            schedules.append(schedule)
            if subject.code == "101" and not schedule.components.exists():
                apply_preset(user=admin, schedule=schedule, key="national-cq-mcq")
        _mark_all(
            admin, schedules, enrollments, lambda s, e, p=pattern: _national_score(levels[e.pk], s.subject.code, p)
        )
        publish_exam(exam, admin)


def seed_programmes(*, school, year, admin, principal, teacher, demo_guardian):
    seed_cambridge(school, year, admin, principal, teacher, demo_guardian)
    seed_ib(school, year, admin)
    seed_national(school, year, admin)
