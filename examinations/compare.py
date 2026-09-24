"""
Recompute and compare: check this system's results against the results another program
produced for the same exam.

Before a school trusts a new results system with a real result day, it wants proof. The
school enters (or imports) one past exam's marks here, uploads what its old software
published for that exam, and gets every difference: each mark, grade and GPA that does not
match, student by student. Nothing is saved; the file is only read.

The file has one row per student and subject: Student ID, Subject (its code, like 101, or its
name), and any of Marks, Grade and GPA. A subject graded on two papers can be given as either
paper or as the subject itself ("Bangla").
"""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from .mark_import import ABSENT_WORDS, _key
from .parts import plain


def template_rows(rows):
    """The class, one row per student and subject, with the columns left for the old results."""
    lines = []
    for row in sorted(rows, key=lambda r: (r["section"], r["roll"])):
        for unit in row.get("subjects") or []:
            lines.append(
                [row["student_code"], row["student"], unit["name"], ", ".join(unit.get("codes") or []), "", "", ""]
            )
    return lines


TEMPLATE_HEADERS = ["Student ID", "Student", "Subject", "Code", "Marks", "Grade", "GPA"]


def _number(text):
    try:
        value = Decimal(str(text).replace(",", ""))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _ours(row, subject):
    """(marks, grade, absent, missing) here for a subject named by code or name; None if not taken here."""
    wanted = subject.strip().casefold()
    for unit in row.get("subjects") or []:
        codes = [c.casefold() for c in unit.get("codes") or []]
        if wanted == unit["name"].casefold() or (len(codes) == 1 and wanted in codes):
            return unit.get("score"), unit.get("letter"), bool(unit.get("absent")), bool(unit.get("missing"))
    for cell in row["cells"]:
        if wanted in ((cell.get("subject_code") or "").casefold(), cell["subject"].casefold()):
            return cell.get("score"), cell.get("letter"), bool(cell.get("absent")), bool(cell.get("missing"))
    return None


def compare(rows, file_rows):
    """
    Every difference between the class's results here (`rows`, as on the cards) and the file.

    Returns (differences, summary): each difference names the student, the subject (or GPA),
    what this system has and what the file says; summary counts students and values compared.
    """
    header_at = next(
        (i for i, line in enumerate(file_rows) if {"student id", "subject"} <= {_key(cell) for cell in line if cell}),
        None,
    )
    if header_at is None:
        raise ValidationError("No heading row with Student ID and Subject was found. Start from the class template.")
    heads = [_key(cell) for cell in file_rows[header_at]]

    def column(name):
        return heads.index(name) if name in heads else None

    id_col, subject_col = column("student id"), column("subject")
    marks_col, grade_col, gpa_col = column("marks"), column("grade"), column("gpa")
    if marks_col is None and grade_col is None and gpa_col is None:
        raise ValidationError("The file needs at least one of the columns Marks, Grade or GPA.")

    by_id = {row["student_code"].casefold(): row for row in rows}
    differences, compared, students, gpa_checked = [], 0, set(), set()

    def differ(row_number, student, subject, what, here, there):
        differences.append(
            {"row": row_number, "student": student, "subject": subject, "what": what, "here": here, "there": there}
        )

    for number, line in enumerate(file_rows[header_at + 1 :], start=header_at + 2):

        def cell(at, line=line):
            return line[at].strip() if at is not None and at < len(line) and line[at] is not None else ""

        student_id, subject = cell(id_col), cell(subject_col)
        if not student_id:
            continue
        row = by_id.get(student_id.casefold())
        if row is None:
            differ(number, student_id, subject, "Student", "not in this class", "in the file")
            continue
        students.add(row["student_code"])
        name = row["student"]
        gpa = cell(gpa_col)
        if gpa and row["student_code"] not in gpa_checked:
            gpa_checked.add(row["student_code"])
            compared += 1
            here = row.get("gpa")
            if here is None or _number(gpa) is None or Decimal(here) != _number(gpa):
                differ(number, name, "GPA", "GPA", here if here is not None else "none", gpa)
        if not subject:
            continue
        ours = _ours(row, subject)
        if ours is None:
            differ(number, name, subject, "Subject", "not taken here", "in the file")
            continue
        score, letter, absent, missing = ours
        marks, grade = cell(marks_col), cell(grade_col)
        if marks:
            compared += 1
            if missing:
                differ(number, name, subject, "Marks", "no mark entered", marks)
            elif marks.lower() in ABSENT_WORDS:
                if not absent:
                    differ(number, name, subject, "Marks", plain(score), "absent")
            elif absent:
                differ(number, name, subject, "Marks", "absent", marks)
            elif _number(marks) is None or Decimal(str(score)) != _number(marks):
                differ(number, name, subject, "Marks", plain(score), marks)
        if grade:
            compared += 1
            if missing or (letter or "").casefold() != grade.casefold():
                differ(number, name, subject, "Grade", "no mark entered" if missing else letter, grade)
    return differences, {"students": len(students), "compared": compared, "differences": len(differences)}
