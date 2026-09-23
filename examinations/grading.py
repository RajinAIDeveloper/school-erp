"""
How a result is worked out.

Two rulebooks live here. The national-curriculum rules are the education boards' own:

- A paper passes only if the student reaches its pass mark overall and in every part
  (creative questions, multiple choice, practical).
- A subject examined in two papers (Bangla 1st and 2nd, English 1st and 2nd) is graded once,
  on the combined marks of both.
- GPA is the average grade point of the main subjects. The 4th subject is left out of that
  average; only its grade point above 2.00 is added, and it can never fail the student.
- A fail in any main subject makes the result a fail and the GPA 0.00. GPA never exceeds 5.00.

The other rulebook is for English-medium, IB and schools with their own scheme: every paper
counts the same, any failed paper fails the result, and nothing is combined or discounted.

Everything here is a plain function of numbers, with no database access, so the rules can be
checked against worked examples one line at a time.
"""

from decimal import ROUND_HALF_UP, Decimal

TWO = Decimal("2.00")
FIVE = Decimal("5.00")
ZERO = Decimal("0")
CENT = Decimal("0.01")


def pct_of(obtained, maximum):
    if not maximum:
        return Decimal("0.00")
    return (Decimal(obtained) / Decimal(maximum) * 100).quantize(CENT, rounding=ROUND_HALF_UP)


def grade_for(percent, rules):
    # Thresholds avoid gaps such as 79.995 between adjacent displayed ranges.
    for rule in sorted(rules, key=lambda r: Decimal(str(r["min_percent"])), reverse=True):
        if percent >= Decimal(str(rule["min_percent"])):
            return rule
    return {"letter": "F", "grade_point": "0"}


def _cap_rule(letter, rules):
    """The scale's rule for a paper's highest allowed grade, matched exactly, then ignoring case."""
    if not letter:
        return None
    for rule in rules:
        if rule["letter"] == letter:
            return rule
    for rule in rules:
        if rule["letter"].lower() == letter.lower():
            return rule
    return None


def failing_letter(rules):
    """The scale's lowest grade: F on the national scale, U on Cambridge and Edexcel scales."""
    if not rules:
        return "F"
    lowest = min(rules, key=lambda r: Decimal(str(r["min_percent"])))
    return lowest["letter"]


def gpa_letter(gpa, rules):
    """The letter a GPA corresponds to: the highest grade whose point the GPA reaches."""
    if gpa is None:
        return None
    for rule in sorted(rules, key=lambda r: Decimal(str(r["grade_point"])), reverse=True):
        if Decimal(str(gpa)) >= Decimal(str(rule["grade_point"])):
            return rule["letter"]
    return "F"


def board_pass_mark(full_marks):
    """33% of a part's marks, rounded the way the boards print them: 70 -> 23, 30 -> 10, 25 -> 8."""
    return (Decimal(full_marks) * Decimal("0.33")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def scale_rules(scale):
    return [
        {
            "letter": r.letter,
            "min_percent": str(r.min_percent),
            "max_percent": str(r.max_percent),
            "grade_point": str(r.grade_point),
        }
        for r in scale.rules.all()
    ]


# ------------------------------------------------------------------------------ papers


def grade_paper(paper, mark, rules, pass_marks=True):
    """
    One paper's cell on a result.

    `paper` is a dict: schedule_id, subject, subject_id, subject_code, unit_id, unit_name,
    full_marks, pass_marks, components [(code, name, full, pass)], role ("main"/"fourth").
    `mark` is None (not entered) or a dict: absent, score, parts {code: score}.

    With `pass_marks`, a paper below its pass mark, or with a part below the part's pass mark,
    fails and takes the scale's failing grade. Without it (Cambridge, Edexcel, IB) there is no
    pass mark at all: the grade is simply the band the percentage falls in, so 45% is an E, not
    a fail. An absence there has no grade.
    """
    missing = mark is None or (not mark["absent"] and mark["score"] is None)
    absent = bool(mark and mark["absent"])
    score = ZERO if missing or absent else Decimal(str(mark["score"]))
    parts = []
    parts_ok = True
    for code, name, full, part_pass in paper["components"]:
        raw = None if (missing or absent) else (mark.get("parts") or {}).get(code)
        got = None if raw in (None, "") else Decimal(str(raw))
        ok = got is not None and got >= Decimal(str(part_pass))
        if not (missing or absent) and not ok:
            parts_ok = False
        parts.append(
            {
                "code": code,
                "name": name,
                "full_marks": str(full),
                "pass_marks": str(part_pass),
                "score": str(got) if got is not None else None,
                "passed": ok,
            }
        )
    percent = pct_of(score, paper["full_marks"])
    band = grade_for(percent, rules)
    capped = False
    cap = _cap_rule(paper.get("max_grade"), rules)
    if cap and Decimal(str(band.get("min_percent", 0))) > Decimal(str(cap["min_percent"])):
        # A tiered paper (Cambridge Core) cannot earn above its tier's top grade.
        band, capped = cap, True
    if pass_marks:
        passed = not missing and not absent and score >= Decimal(str(paper["pass_marks"])) and parts_ok
        letter = band["letter"] if passed else failing_letter(rules)
        grade_point = Decimal(str(band["grade_point"])) if passed else ZERO
    else:
        passed = not missing and not absent
        letter = "ABS" if absent else band["letter"]
        grade_point = ZERO if (absent or missing) else Decimal(str(band["grade_point"]))
    if paper.get("core") == "cas":
        # CAS has no grade: it is complete or it is not.
        reached = not missing and not absent and score >= Decimal(str(paper["pass_marks"]))
        letter = "Complete" if reached else ("ABS" if absent else "Not complete")
        grade_point = ZERO
    return {
        "schedule_id": paper["schedule_id"],
        "subject": paper["subject"],
        "subject_id": paper["subject_id"],
        "subject_code": paper.get("subject_code", ""),
        "unit_id": paper["unit_id"],
        "unit_name": paper["unit_name"],
        "full_marks": str(paper["full_marks"]),
        "pass_marks": str(paper["pass_marks"]),
        "score": str(score) if not missing and not absent else None,
        "missing": missing,
        "absent": absent,
        "percent": str(percent),
        "letter": letter,
        "grade_point": str(grade_point),
        "passed": passed,
        "failed_part": bool(pass_marks and not parts_ok),
        "capped": capped,
        "components": parts,
        "is_fourth": paper["role"] == "fourth",
        "level": paper.get("level", ""),
        "core": paper.get("core", ""),
        # Whether the raw mark reached the paper's pass mark, whatever the rulebook does with
        # it. The IB core uses it for CAS: complete or not.
        "reached_pass_mark": not missing and not absent and score >= Decimal(str(paper["pass_marks"])),
    }


# ------------------------------------------------------------------------------ subjects


def combine_units(cells, rules, combine=True):
    """
    Group paper cells into the subjects a GPA is counted over.

    With `combine`, papers sharing a unit (Bangla 1st and 2nd paper) are graded once on their
    combined marks, and each part must reach its combined pass mark: the creative scores of
    both papers against both creative pass marks, and so on. Without it, every paper is its
    own subject.
    """
    order, grouped = [], {}
    for cell in cells:
        key = cell["unit_id"] if combine else f"paper-{cell['schedule_id']}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(cell)

    units = []
    for key in order:
        papers = grouped[key]
        if len(papers) == 1:
            cell = papers[0]
            units.append(
                {
                    "name": cell["unit_name"] if combine else cell["subject"],
                    "codes": [cell["subject_code"]] if cell["subject_code"] else [],
                    "papers": [cell["schedule_id"]],
                    "full_marks": cell["full_marks"],
                    "score": cell["score"],
                    "missing": cell["missing"],
                    "absent": cell["absent"],
                    "percent": cell["percent"],
                    "letter": cell["letter"],
                    "grade_point": cell["grade_point"],
                    "passed": cell["passed"],
                    "is_fourth": cell["is_fourth"],
                    "level": cell.get("level", ""),
                    "core": cell.get("core", ""),
                    "reached_pass_mark": cell.get("reached_pass_mark", cell["passed"]),
                }
            )
            continue
        missing = any(c["missing"] for c in papers)
        absent = not missing and any(c["absent"] for c in papers)
        full = sum((Decimal(c["full_marks"]) for c in papers), ZERO)
        pass_total = sum((Decimal(c["pass_marks"]) for c in papers), ZERO)
        score = ZERO if missing or absent else sum((Decimal(c["score"]) for c in papers), ZERO)
        parts_ok = True
        if not (missing or absent):
            by_code = {}
            for cell in papers:
                for part in cell["components"]:
                    got, need = by_code.get(part["code"], (ZERO, ZERO))
                    by_code[part["code"]] = (
                        got + Decimal(part["score"] or 0),
                        need + Decimal(part["pass_marks"]),
                    )
            parts_ok = all(got >= need for got, need in by_code.values())
        passed = not missing and not absent and score >= pass_total and parts_ok
        percent = pct_of(score, full)
        band = grade_for(percent, rules)
        failing = failing_letter(rules)
        units.append(
            {
                "name": papers[0]["unit_name"],
                "codes": [c["subject_code"] for c in papers if c["subject_code"]],
                "papers": [c["schedule_id"] for c in papers],
                "full_marks": str(full),
                "score": str(score) if not missing and not absent else None,
                "missing": missing,
                "absent": absent,
                "percent": str(percent),
                "letter": band["letter"] if passed else failing,
                "grade_point": str(Decimal(str(band["grade_point"])) if passed else ZERO),
                "passed": passed,
                "is_fourth": any(c["is_fourth"] for c in papers),
            }
        )
    return units


# ------------------------------------------------------------------------------ outcome


def national_outcome(units, rules):
    """
    GPA, result and letter under the boards' rules.

    Returns complete, result ("PASS"/"FAIL"/"INCOMPLETE"), gpa, gpa_without_fourth, gpa_letter.
    """
    main = [u for u in units if not u["is_fourth"]]
    fourth = [u for u in units if u["is_fourth"]]
    complete = bool(main) and not any(u["missing"] for u in units)
    if not complete:
        return _incomplete(units)
    failed = [u["name"] for u in main if not u["passed"]]
    if failed:
        return _outcome(
            result="FAIL",
            gpa=Decimal("0.00"),
            gpa_without_fourth=Decimal("0.00"),
            gpa_letter="F",
            headline="GPA 0.00 · FAIL",
            trace=[f"Failed main subject(s): {', '.join(failed)}. Any failed main subject makes the GPA 0.00."],
        )
    base = sum((Decimal(u["grade_point"]) for u in main), ZERO)
    # Only the part of the 4th subject's grade point above 2.00 counts, and a failed or absent
    # 4th subject simply adds nothing.
    bonus = sum((max(ZERO, Decimal(u["grade_point"]) - TWO) for u in fourth), ZERO)
    gpa = min(FIVE, (base + bonus) / len(main)).quantize(CENT, rounding=ROUND_HALF_UP)
    without = min(FIVE, base / len(main)).quantize(CENT, rounding=ROUND_HALF_UP)
    trace = [f"Main subjects: {' + '.join(u['grade_point'] for u in main)} = {base} over {len(main)} subjects."]
    for u in fourth:
        added = max(ZERO, Decimal(u["grade_point"]) - TWO)
        trace.append(
            f"4th subject {u['name']}: grade point {u['grade_point']}; the part above 2.00, {added}, is added."
        )
    trace.append(f"GPA = ({base} + {bonus}) / {len(main)} = {gpa}, capped at 5.00.")
    return _outcome(
        result="PASS",
        gpa=gpa,
        gpa_without_fourth=without,
        gpa_letter=gpa_letter(gpa, rules),
        headline=f"GPA {gpa} · PASS",
        trace=trace,
    )


def _outcome(**values):
    base = {
        "complete": True,
        "result": None,
        "gpa": None,
        "gpa_without_fourth": None,
        "gpa_letter": None,
        "points": None,
        "headline": "",
        "trace": [],
    }
    base.update(values)
    return base


def _incomplete(units):
    missing = [u["name"] for u in units if u["missing"]]
    return _outcome(
        complete=False,
        result="INCOMPLETE",
        headline="Incomplete",
        trace=[f"Not yet entered: {', '.join(missing)}." if missing else "No papers to grade."],
    )


def standard_outcome(units, rules):
    """The school's own rules: every paper counts the same, and any failed paper fails the result."""
    complete = bool(units) and not any(u["missing"] for u in units)
    if not complete:
        return _incomplete(units)
    points = [Decimal(u["grade_point"]) for u in units]
    failed = [u["name"] for u in units if not u["passed"]]
    gpa = Decimal("0.00") if failed else (sum(points) / len(points)).quantize(CENT)
    trace = [f"Grade points {' + '.join(str(p) for p in points)} over {len(points)} paper(s)."]
    if failed:
        trace.append(f"Failed: {', '.join(failed)}.")
    return _outcome(
        result="FAIL" if failed else "PASS",
        gpa=gpa,
        gpa_without_fourth=gpa,
        gpa_letter="F" if failed else gpa_letter(gpa, rules),
        headline=f"GPA {gpa} · {'FAIL' if failed else 'PASS'}",
        trace=trace,
    )


def grade_tally(units):
    """'3 A*, 2 A, 1 B': how many of each grade, best grade first."""
    order, counts = [], {}
    for u in sorted(units, key=lambda u: Decimal(u["percent"] or 0), reverse=True):
        letter = u["letter"]
        if letter not in counts:
            order.append(letter)
            counts[letter] = 0
        counts[letter] += 1
    return ", ".join(f"{counts[letter]} {letter}" for letter in order)


def grades_outcome(units, rules):
    """
    Cambridge and Pearson Edexcel: a grade per subject from the board's scale, and nothing more.

    These programmes have no GPA and no overall pass or fail, so none is invented. The headline
    is the tally of grades, the way these schools describe results ("five A*s"). Internal exams
    use the school's own thresholds; the awarding bodies set official thresholds per exam
    series, and an official result is recorded as imported, never calculated here.
    """
    complete = bool(units) and not any(u["missing"] for u in units)
    if not complete:
        return _incomplete(units)
    graded = [u for u in units if not u["absent"]]
    absent = [u["name"] for u in units if u["absent"]]
    trace = [f"{u['name']}: {u['percent']}% gives {u['letter']}." for u in graded]
    if absent:
        trace.append(f"Absent: {', '.join(absent)}; no grade awarded.")
    return _outcome(headline=grade_tally(graded) or "No grades", trace=trace)


def myp_outcome(units, rules):
    """
    IB Middle Years: a subject's four criteria (0-8 each) sum to 0-32, and the total becomes a
    grade from 1 to 7 by the published boundaries. The headline is the sum of subject grades,
    which is how the MYP certificate is judged.
    """
    complete = bool(units) and not any(u["missing"] for u in units)
    if not complete:
        return _incomplete(units)
    graded = [u for u in units if not u["absent"]]
    total = sum((int(Decimal(u["grade_point"])) for u in graded), 0)
    trace = [f"{u['name']}: {u['score']} of {u['full_marks']} gives grade {u['letter']}." for u in graded]
    trace.append(f"Sum of subject grades: {total}.")
    return _outcome(points=total, headline=f"{total} points across {len(graded)} subject(s)", trace=trace)


# Diploma core points from the Extended Essay (rows) and TOK (columns). None is a failing
# condition. Source: the IB's DP passing criteria, checked on ibo.org, September 2026.
DP_CORE_MATRIX = {
    "A": {"A": 3, "B": 3, "C": 2, "D": 2, "E": None},
    "B": {"A": 3, "B": 2, "C": 2, "D": 1, "E": None},
    "C": {"A": 2, "B": 2, "C": 1, "D": 0, "E": None},
    "D": {"A": 2, "B": 1, "C": 0, "D": 0, "E": None},
    "E": {"A": None, "B": None, "C": None, "D": None, "E": None},
}


def dp_core_points(ee, tok):
    """Core points for an EE and a TOK grade, or None when either is an E (a failing condition)."""
    return DP_CORE_MATRIX.get(ee, {}).get(tok)


def dp_outcome(units, rules):
    """
    IB Diploma, as the school's estimate from its own marks.

    Six subjects graded 1-7, plus core points from TOK and the Extended Essay, and the IB's
    eight conditions for the diploma. The result is labelled a school estimate throughout:
    the diploma itself is awarded by the IB, and an official result is recorded as imported.
    """
    subjects = [u for u in units if not u.get("core")]
    core = {u["core"]: u for u in units if u.get("core")}
    trace, unmet = [], []

    missing = [u["name"] for u in units if u["missing"]]
    if missing:
        return _incomplete(units)

    # Condition 3: a grade in every subject, TOK and the EE. An absence is an N.
    no_grade = [u["name"] for u in subjects if u["absent"]]
    for key, label in (("tok", "TOK"), ("ee", "Extended Essay")):
        if key not in core:
            unmet.append(f"{label} is not recorded, so the diploma cannot be confirmed.")
        elif core[key]["absent"]:
            no_grade.append(label)
    if no_grade:
        unmet.append(f"No grade (N) in: {', '.join(no_grade)}.")

    grades = {u["name"]: int(Decimal(u["grade_point"])) for u in subjects if not u["absent"]}
    subject_points = sum(grades.values())
    trace.append(f"Subject grades: {' + '.join(str(g) for g in grades.values())} = {subject_points}.")

    core_points = 0
    ee = core.get("ee", {}).get("letter")
    tok = core.get("tok", {}).get("letter")
    if ee and tok and ee != "ABS" and tok != "ABS":
        points = dp_core_points(ee, tok)
        if points is None:
            unmet.append(f"An E in TOK or the Extended Essay (EE {ee}, TOK {tok}) is a failing condition.")
        else:
            core_points = points
            trace.append(f"Core: Extended Essay {ee} with TOK {tok} gives {points} point(s).")

    total = subject_points + core_points
    trace.append(f"Total: {subject_points} + {core_points} = {total} of 45.")

    # Condition 1: CAS.
    cas = core.get("cas")
    if cas is None:
        unmet.append("CAS is not recorded, so the diploma cannot be confirmed.")
    elif not cas.get("reached_pass_mark"):
        unmet.append("CAS requirements are not met.")
    # Condition 2: at least 24 points.
    if total < 24:
        unmet.append(f"{total} points is below the minimum of 24.")
    values = list(grades.values())
    # Condition 4: at least a 2 in every subject.
    ones = [name for name, grade in grades.items() if grade < 2]
    if ones:
        unmet.append(f"A grade 1 in: {', '.join(ones)}.")
    # Condition 5: no more than two grade 2s.
    if sum(1 for g in values if g == 2) > 2:
        unmet.append("More than two grade 2s.")
    # Condition 6: no more than three grades of 3 or below.
    if sum(1 for g in values if g <= 3) > 3:
        unmet.append("More than three grades of 3 or below.")
    # Condition 7: at least 12 points on HL (best three when there are four).
    hl = sorted((grades[u["name"]] for u in subjects if u.get("level") == "HL" and u["name"] in grades), reverse=True)
    if hl:
        hl_points = sum(hl[:3])
        trace.append(f"HL points (best three): {hl_points}.")
        if hl_points < 12:
            unmet.append(f"{hl_points} points on HL subjects; at least 12 are needed.")
    # Condition 8: at least 9 on SL, or at least 5 when there are only two SL subjects.
    sl = [grades[u["name"]] for u in subjects if u.get("level") == "SL" and u["name"] in grades]
    if sl:
        need = 5 if len(sl) == 2 else 9
        trace.append(f"SL points: {sum(sl)} (at least {need} needed).")
        if sum(sl) < need:
            unmet.append(f"{sum(sl)} points on SL subjects; at least {need} are needed.")
    if not hl and not sl:
        unmet.append("No subject is marked HL or SL, so the level conditions cannot be checked.")

    trace.extend(unmet)
    status = "diploma conditions met" if not unmet else "diploma conditions not met"
    return _outcome(points=total, headline=f"{total} points · {status} (school estimate)", trace=trace)


def merit_key(row):
    """
    Order for a merit list: passes before fails, then GPA, then total marks.

    That is how the boards rank, and it means a failing student can never outrank a passing
    one on marks alone.
    """
    return (
        row["result"] == "PASS",
        Decimal(row["gpa"] or 0),
        Decimal(row["total"]),
    )


def headline(row):
    """
    One line for a student's overall result, whatever the rulebook.

    Newer results carry it; results published before rulebooks existed are described from
    their GPA and pass or fail, exactly as they were shown then.
    """
    if not row:
        return ""
    if row.get("headline"):
        return row["headline"]
    gpa, result = row.get("gpa"), row.get("result")
    if result == "INCOMPLETE":
        return "Incomplete"
    return " · ".join(part for part in (f"GPA {gpa}" if gpa is not None else "", result or "") if part)


def shows_rank(row):
    """Whether positions are printed for this result. Older results always showed them."""
    return bool(row) and row.get("show_rank", True)


def points_key(row):
    """IB order: total points, then total marks."""
    return (Decimal(row.get("points") or 0), Decimal(row["total"]))


def percent_key(row):
    """For grade-only programmes, where a position is wanted at all: the average percentage."""
    return (Decimal(row.get("percent") or 0),)
