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


def grade_paper(paper, mark, rules):
    """
    One paper's cell on a result.

    `paper` is a dict: schedule_id, subject, subject_id, subject_code, unit_id, unit_name,
    full_marks, pass_marks, components [(code, name, full, pass)], role ("main"/"fourth").
    `mark` is None (not entered) or a dict: absent, score, parts {code: score}.
    """
    missing = mark is None or (not mark["absent"] and mark["score"] is None)
    absent = bool(mark and mark["absent"])
    score = ZERO if missing or absent else Decimal(str(mark["score"]))
    parts = []
    parts_ok = True
    for code, name, full, pass_marks in paper["components"]:
        raw = None if (missing or absent) else (mark.get("parts") or {}).get(code)
        got = None if raw in (None, "") else Decimal(str(raw))
        ok = got is not None and got >= Decimal(str(pass_marks))
        if not (missing or absent) and not ok:
            parts_ok = False
        parts.append(
            {
                "code": code,
                "name": name,
                "full_marks": str(full),
                "pass_marks": str(pass_marks),
                "score": str(got) if got is not None else None,
                "passed": ok,
            }
        )
    passed = not missing and not absent and score >= Decimal(str(paper["pass_marks"])) and parts_ok
    percent = pct_of(score, paper["full_marks"])
    band = grade_for(percent, rules)
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
        "letter": band["letter"] if passed else "F",
        "grade_point": str(Decimal(str(band["grade_point"])) if passed else ZERO),
        "passed": passed,
        "failed_part": not parts_ok,
        "components": parts,
        "is_fourth": paper["role"] == "fourth",
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
                "letter": band["letter"] if passed else "F",
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
        return {"complete": False, "result": "INCOMPLETE", "gpa": None, "gpa_without_fourth": None, "gpa_letter": None}
    if any(not u["passed"] for u in main):
        return {
            "complete": True,
            "result": "FAIL",
            "gpa": Decimal("0.00"),
            "gpa_without_fourth": Decimal("0.00"),
            "gpa_letter": "F",
        }
    base = sum((Decimal(u["grade_point"]) for u in main), ZERO)
    # Only the part of the 4th subject's grade point above 2.00 counts, and a failed or absent
    # 4th subject simply adds nothing.
    bonus = sum((max(ZERO, Decimal(u["grade_point"]) - TWO) for u in fourth), ZERO)
    gpa = min(FIVE, (base + bonus) / len(main)).quantize(CENT, rounding=ROUND_HALF_UP)
    without = min(FIVE, base / len(main)).quantize(CENT, rounding=ROUND_HALF_UP)
    return {
        "complete": True,
        "result": "PASS",
        "gpa": gpa,
        "gpa_without_fourth": without,
        "gpa_letter": gpa_letter(gpa, rules),
    }


def standard_outcome(units, rules):
    """Every paper counts the same, and any failed paper fails the result."""
    complete = bool(units) and not any(u["missing"] for u in units)
    if not complete:
        return {"complete": False, "result": "INCOMPLETE", "gpa": None, "gpa_without_fourth": None, "gpa_letter": None}
    points = [Decimal(u["grade_point"]) for u in units]
    failed = any(not u["passed"] for u in units)
    gpa = Decimal("0.00") if failed else (sum(points) / len(points)).quantize(CENT)
    return {
        "complete": True,
        "result": "FAIL" if failed else "PASS",
        "gpa": gpa,
        "gpa_without_fourth": gpa,
        "gpa_letter": "F" if failed else gpa_letter(gpa, rules),
    }


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
