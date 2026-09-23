"""
Grade scales a school can start from, one per programme.

The letters and their order come from each awarding body. The percentage thresholds do not:
Cambridge and Pearson Edexcel set official thresholds separately for every exam series, and a
school's internal exams use the school's own. So every preset below except the IB MYP one is a
starting point to edit, and says so. The MYP boundaries are the IB's published conversion of a
criterion total out of 32 into a grade.
"""

from decimal import Decimal

from django.db import transaction

from .models import GradeRule, GradeScale

# key: (name, note, [(letter, min_percent, grade_point), ...] highest first)
PRESETS = {
    "bd-national": (
        "Bangladesh national (A+ to F)",
        "The education boards' scale: A+ 80-100 (5.00) down to F below 33 (0.00).",
        [("A+", 80, 5), ("A", 70, 4), ("A-", 60, "3.5"), ("B", 50, 3), ("C", 40, 2), ("D", 33, 1), ("F", 0, 0)],
    ),
    "cambridge-igcse": (
        "Cambridge IGCSE (A* to G)",
        "Letters are Cambridge's; the thresholds are typical internal ones. Replace them with your school's.",
        [
            ("A*", 90, 8),
            ("A", 80, 7),
            ("B", 70, 6),
            ("C", 60, 5),
            ("D", 50, 4),
            ("E", 40, 3),
            ("F", 30, 2),
            ("G", 20, 1),
            ("U", 0, 0),
        ],
    ),
    "igcse-9-1": (
        "International GCSE (9 to 1)",
        "For Edexcel International GCSE and Cambridge 9-1 syllabuses. 4 is a standard pass, 5 a strong "
        "pass. The thresholds are typical internal ones; replace them with your school's.",
        [
            ("9", 90, 9),
            ("8", 80, 8),
            ("7", 70, 7),
            ("6", 60, 6),
            ("5", 50, 5),
            ("4", 40, 4),
            ("3", 30, 3),
            ("2", 20, 2),
            ("1", 10, 1),
            ("U", 0, 0),
        ],
    ),
    "o-level": (
        "Cambridge O Level (A* to E)",
        "Letters are Cambridge's; the thresholds are typical internal ones. Replace them with your school's.",
        [("A*", 90, 6), ("A", 80, 5), ("B", 70, 4), ("C", 60, 3), ("D", 50, 2), ("E", 40, 1), ("U", 0, 0)],
    ),
    "a-level": (
        "A Level (A* to E)",
        "For full Cambridge and Edexcel International A Level. A* exists only at full A Level, never on "
        "a single component. The thresholds are typical internal ones; replace them with your school's.",
        [("A*", 90, 6), ("A", 80, 5), ("B", 70, 4), ("C", 60, 3), ("D", 50, 2), ("E", 40, 1), ("U", 0, 0)],
    ),
    "as-level": (
        "AS Level (a to e)",
        "AS grades are written in lower case, a to e, and there is no a*. The thresholds are typical "
        "internal ones; replace them with your school's.",
        [("a", 80, 5), ("b", 70, 4), ("c", 60, 3), ("d", 50, 2), ("e", 40, 1), ("u", 0, 0)],
    ),
    "ib-1-7": (
        "IB grades (1 to 7)",
        "For DP subjects and other 1-7 reporting. The IB sets official boundaries per subject and "
        "session; these are typical internal ones. Replace them with your school's.",
        [("7", 80, 7), ("6", 70, 6), ("5", 60, 5), ("4", 50, 4), ("3", 40, 3), ("2", 25, 2), ("1", 0, 1)],
    ),
    "ib-myp": (
        "IB MYP criteria (total out of 32 to 1-7)",
        "The IB's conversion of a criterion total out of 32: 1-5 = 1, 6-9 = 2, 10-14 = 3, 15-18 = 4, "
        "19-23 = 5, 24-27 = 6, 28-32 = 7. Use with papers out of 32 marked in four criteria.",
        [
            ("7", Decimal("87.50"), 7),  # 28 of 32
            ("6", Decimal("75.00"), 6),  # 24
            ("5", Decimal("59.38"), 5),  # 19 (59.375, rounded as percentages are)
            ("4", Decimal("46.88"), 4),  # 15 (46.875)
            ("3", Decimal("31.25"), 3),  # 10
            ("2", Decimal("18.75"), 2),  # 6
            ("1", Decimal("0"), 1),
        ],
    ),
}


@transaction.atomic
def install_preset(school, key):
    """
    Create a preset scale for a school, or return it untouched if it already exists.

    An existing scale is never overwritten: a school may have edited the thresholds, and
    published results keep their own frozen copy regardless.
    """
    name, note, bands = PRESETS[key]
    scale, created = GradeScale.objects.get_or_create(school=school, name=name)
    if not created and scale.rules.exists():
        return scale, False
    upper = Decimal("100")
    for letter, min_percent, points in bands:
        low = Decimal(str(min_percent))
        GradeRule.objects.create(
            scale=scale, letter=letter, min_percent=low, max_percent=upper, grade_point=Decimal(str(points))
        )
        upper = (low - Decimal("0.01")).quantize(Decimal("0.01"))
    return scale, True


def preset_choices():
    return [(key, name) for key, (name, _note, _bands) in PRESETS.items()]


def preset_notes():
    return {key: note for key, (_name, note, _bands) in PRESETS.items()}
