"""
Class result exports: the class sheet, grade distribution per subject, the national
tabulation sheet and merit lists.

Every export is built from the same rows the report cards print: the published snapshot, or
for an unpublished exam the live results marked as a draft. So a total on an export always
matches the card. Each export says which exam, which published version, which rulebook and
which filter it covers, and every percentage says what it is a percentage of.
"""

from decimal import Decimal

from .grading import headline
from .rulebooks import rulebook
from .services import assign_ranks

REPORTS = [
    ("sheet", "Class results sheet"),
    ("distribution", "Grade distribution by subject"),
    ("tabulation", "Tabulation sheet (national curriculum)"),
    ("merit", "Merit list"),
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
        ("Rulebook", sheet["rulebook"].label),
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


def _letters_in_order(sheet, rows):
    """The scale's letters highest first, then any others seen (a paper on another scale)."""
    exam = sheet["exam"]
    rules = exam.grading_snapshot or []
    if not rules and exam.grade_scale_id:
        from .grading import scale_rules

        rules = scale_rules(exam.grade_scale)
    ordered = [r["letter"] for r in sorted(rules, key=lambda r: Decimal(str(r["min_percent"])), reverse=True)]
    for row in rows:
        for unit in row.get("subjects") or []:
            letter = unit.get("letter")
            if letter and not unit.get("missing") and not unit.get("absent") and letter not in ordered:
                ordered.append(letter)
    return ordered


def grade_distribution(sheet, rows):
    """
    Per subject: how many sat it, how many were absent or have no mark yet, and how many earned
    each grade. Percentages are of those who sat the subject, never of the whole class, so a
    subject only some students take is not diluted by those who do not take it.

    Returns (letters, lines) where each line has subject, sat, absent, missing, counts {letter:
    n} and at_or_above {letter: percent}.
    """
    letters = _letters_in_order(sheet, rows)
    lines = {}
    for row in rows:
        for unit in row.get("subjects") or []:
            if unit.get("core") == "cas":
                continue
            line = lines.setdefault(
                unit["name"],
                {"subject": unit["name"], "sat": 0, "absent": 0, "missing": 0, "counts": dict.fromkeys(letters, 0)},
            )
            if unit.get("missing"):
                line["missing"] += 1
            elif unit.get("absent"):
                line["absent"] += 1
            else:
                line["sat"] += 1
                line["counts"][unit["letter"]] = line["counts"].get(unit["letter"], 0) + 1
    for line in lines.values():
        running, line["at_or_above"] = 0, {}
        for letter in letters:
            running += line["counts"].get(letter, 0)
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
