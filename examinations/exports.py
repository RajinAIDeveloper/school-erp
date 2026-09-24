"""
Class result exports: the class sheet, grade distribution per subject, the national
tabulation sheet, merit lists, and the lists of who failed and who is near the pass mark.

Every export is built from the same rows the report cards print: the published snapshot, or
for an unpublished exam the live results marked as a draft. So a total on an export always
matches the card. Each export says which exam, which published version, which rulebook and
which filter it covers, and every percentage says what it is a percentage of.
"""

from collections import Counter
from decimal import Decimal

from .grading import headline
from .rulebooks import rulebook
from .services import assign_ranks

REPORTS = [
    ("sheet", "Class results sheet"),
    ("distribution", "Grade distribution by subject"),
    ("tabulation", "Tabulation sheet (national curriculum)"),
    ("merit", "Merit list"),
    ("fails", "Failed subjects"),
    ("nearfail", "Near the pass mark"),
]


def about(sheet, filters=""):
    """The lines that say what an export covers."""
    exam = sheet["exam"]
    version = (
        f"Published version {exam.publication_version}"
        if exam.status == "published"
        else "Draft: not published, and may still change"
    )
    scope = str(sheet["class_level"]) + (f", section {sheet['section'].name}" if sheet["section"] else "")
    lines = [
        ("Exam", f"{exam.name} ({exam.academic_year})"),
        ("Class", scope),
        ("Version", version),
        ("Rulebook", f"{sheet['rulebook'].label}, rules version {sheet['rulebook'].version}"),
    ]
    if filters:
        lines.append(("Filter", filters))
    lines.append(("Students", str(len(sheet["rows"]))))
    return lines


def filter_rows(rows, *, group="", shift="", version=""):
    """
    Keep only the students in one group, shift or version. The values are the stored labels
    ("Science", "Morning"), matched against the row as it was published.
    """
    kept = rows
    for field, value in (("group", group), ("shift", shift), ("version", version)):
        if value:
            kept = [r for r in kept if (r.get(field) or "") == value]
    return kept


# ------------------------------------------------------------------ grade distribution


def _ordered(rules):
    return [r["letter"] for r in sorted(rules, key=lambda r: Decimal(str(r["min_percent"])), reverse=True)]


def _exam_letters(sheet):
    exam = sheet["exam"]
    rules = exam.grading_snapshot or []
    if not rules and exam.grade_scale_id:
        from .grading import scale_rules

        rules = scale_rules(exam.grade_scale)
    return _ordered(rules)


def _subject_letters(row, unit, exam_letters):
    """
    The grade order for one subject: its paper's own scale when it has one (Edexcel 9-1
    Mathematics in a Cambridge exam), otherwise the exam's.
    """
    scales = (row.get("policy") or {}).get("paper_scales") or {}
    for paper in unit.get("papers") or []:
        rules = (scales.get(str(paper)) or {}).get("rules")
        if rules:
            return _ordered(rules)
    return list(exam_letters)


def grade_distribution(sheet, rows):
    """
    Per subject: how many sat it, how many were absent or have no mark yet, and how many earned
    each grade. Percentages are of those who sat the subject, never of the whole class, so a
    subject only some students take is not diluted by those who do not take it.

    Each subject is ordered by its own scale, so "at or above" is always worked down that
    scale's grades. A grade from another scale is left blank on that subject's line, not zero.

    Returns (letters, lines): letters is every grade column (the exam's scale first, then any
    other scale's in its own order); each line has subject, sat, absent, missing, order,
    counts {letter: n} and at_or_above {letter: percent}.
    """
    exam_letters = _exam_letters(sheet)
    lines = {}
    for row in rows:
        for unit in row.get("subjects") or []:
            if unit.get("core") == "cas":
                continue
            if unit["name"] not in lines:
                order = _subject_letters(row, unit, exam_letters)
                lines[unit["name"]] = {
                    "subject": unit["name"],
                    "sat": 0,
                    "absent": 0,
                    "missing": 0,
                    "order": order,
                    "counts": dict.fromkeys(order, 0),
                }
            line = lines[unit["name"]]
            if unit.get("missing"):
                line["missing"] += 1
            elif unit.get("absent"):
                line["absent"] += 1
            else:
                line["sat"] += 1
                if unit["letter"] not in line["counts"]:
                    line["order"].append(unit["letter"])
                    line["counts"][unit["letter"]] = 0
                line["counts"][unit["letter"]] += 1
    letters = list(exam_letters)
    for line in lines.values():
        letters += [x for x in line["order"] if x not in letters]
        running, line["at_or_above"] = 0, {}
        for letter in line["order"]:
            running += line["counts"][letter]
            line["at_or_above"][letter] = round(running * 100 / line["sat"], 1) if line["sat"] else None
    return letters, list(lines.values())


# ------------------------------------------------------------------ tabulation


def tabulation(rows):
    """
    The national curriculum's tabulation sheet: each subject's marks, letter grade and grade
    point, then GPA with and without the 4th subject and the result. Combined papers appear
    once, as the subject they count towards.
    """
    subjects = []
    for row in rows:
        for unit in row.get("subjects") or []:
            if unit["name"] not in subjects:
                subjects.append(unit["name"])
    headers = ["Roll", "Student", "Group", "4th subject"]
    for name in subjects:
        headers += [f"{name} marks", f"{name} LG", f"{name} GP"]
    headers += ["GPA", "GPA without 4th", "Result"]
    body = []
    for row in sorted(rows, key=lambda r: (r["section"], r["roll"])):
        by_name = {unit["name"]: unit for unit in row.get("subjects") or []}
        line = [row["roll"], row["student"], row.get("group", ""), row.get("fourth_subject", "")]
        for name in subjects:
            unit = by_name.get(name)
            if unit is None:
                line += ["", "", ""]
            elif unit.get("missing"):
                line += ["—", "—", "—"]
            elif unit.get("absent"):
                line += ["ABS", unit["letter"], unit["grade_point"]]
            else:
                line += [unit["score"], unit["letter"], unit["grade_point"]]
        line += [row.get("gpa") or "", row.get("gpa_without_fourth") or "", row.get("result") or ""]
        body.append(line)
    return headers, body


# ------------------------------------------------------------------ failed and near the pass mark


def _counted(unit):
    return not unit.get("missing") and unit.get("core") != "cas"


def _pass_mark(row, unit):
    """A subject's pass mark: its paper's, or for papers graded together the sum of theirs."""
    cells = {cell["schedule_id"]: cell for cell in row["cells"]}
    return sum((Decimal(str(cells[pk]["pass_marks"])) for pk in unit["papers"] if pk in cells), Decimal(0))


def failed_subjects(rows):
    """
    Students who failed one subject or more, most failures first, with the subjects named.

    An absence fails the subject, as it does on the card. The national 4th subject never fails
    a student, so it is left out, and a subject still without a mark is not counted yet.

    Returns (headers, body, counts), where counts maps a number of failed subjects to how many
    students failed that many.
    """
    found = []
    for row in rows:
        failed = [
            unit["name"]
            for unit in row.get("subjects") or []
            if _counted(unit) and not unit.get("passed") and not unit.get("is_fourth")
        ]
        if failed:
            found.append((len(failed), row, failed))
    found.sort(key=lambda item: (-item[0], item[1]["section"], item[1]["roll"]))
    headers = ["Subjects failed", "Section", "Roll", "Student", "Group", "Failed in"]
    body = [[n, r["section"], r["roll"], r["student"], r.get("group", ""), ", ".join(names)] for n, r, names in found]
    return headers, body, Counter(n for n, _row, _names in found)


def near_pass_mark(rows, margin):
    """
    Every subject score within `margin` marks of its pass mark, above or below: the students a
    little help would move. A score with enough marks overall that still failed on a part (the
    multiple-choice paper, say) is listed as such.
    """
    from .parts import plain

    found = []
    for row in rows:
        for unit in row.get("subjects") or []:
            if not _counted(unit) or unit.get("absent") or unit.get("score") is None:
                continue
            gap = Decimal(str(unit["score"])) - _pass_mark(row, unit)
            if abs(gap) > margin:
                continue
            if unit.get("passed"):
                standing = f"Passed by {plain(gap)}"
            elif gap >= 0:
                standing = "Enough marks, but failed a part"
            else:
                standing = f"Short by {plain(-gap)}"
            name = unit["name"] + (" (4th subject)" if unit.get("is_fourth") else "")
            found.append((name, gap, row, unit, standing))
    found.sort(key=lambda item: (item[0], item[1], item[2]["section"], item[2]["roll"]))
    headers = ["Subject", "Section", "Roll", "Student", "Marks", "Pass mark", "Standing"]
    body = [
        [name, r["section"], r["roll"], r["student"], plain(u["score"]), plain(_pass_mark(r, u)), standing]
        for name, _gap, r, u, standing in found
    ]
    return headers, body


# ------------------------------------------------------------------ merit list


def merit_list(rows):
    """
    Positions among exactly the students shown, by the rulebook's own order. Students without
    a complete result are listed after, unranked, and counted separately.

    Returns (ranked, unranked) as lists of rows with a "merit" position.
    """
    if not rows:
        return [], []
    subset = [dict(r) for r in rows]
    assign_ranks(subset, "merit", sort_key=rulebook(subset[0]["system"]).rank_key)
    ranked = sorted((r for r in subset if r["merit"] is not None), key=lambda r: (r["merit"], r["section"], r["roll"]))
    unranked = sorted((r for r in subset if r["merit"] is None), key=lambda r: (r["section"], r["roll"]))
    return ranked, unranked


def merit_table(book, ranked, unranked):
    headers = ["Position", "Student", "Section", "Roll", "Group", "Total", "Percent"]
    headers += ["GPA"] if book.has_gpa else (["Points"] if any(r.get("points") is not None for r in ranked) else [])
    headers += ["Result"] if book.has_result else ["Overall"]
    body = []
    for row in [*ranked, *unranked]:
        line = [
            row["merit"] if row["merit"] is not None else "Not ranked",
            row["student"],
            row["section"],
            row["roll"],
            row.get("group", ""),
            row["total"],
            row["percent"] or "",
        ]
        if book.has_gpa:
            line.append(row.get("gpa") or "")
        elif "Points" in headers:
            line.append(row.get("points") if row.get("points") is not None else "")
        line.append((row.get("result") or "") if book.has_result else headline(row))
        body.append(line)
    return headers, body
