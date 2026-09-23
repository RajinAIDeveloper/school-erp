"""
The rulebooks a result can follow.

A rulebook is chosen per school, per class or per exam, and decides four things:

- whether a paper has a pass mark that turns a low score into a fail;
- whether two papers of one subject are graded together (Bangla 1st and 2nd paper);
- whether a 4th subject is treated specially;
- how the per-subject results become the overall result, and whether positions are shown.

Nothing about one programme leaks into another: a Cambridge class never gets a GPA, a national
class never loses its 4th-subject rule, and a school that never chooses keeps its own rules,
which are how the system has always worked.
"""

from collections.abc import Callable
from dataclasses import dataclass

from .grading import (
    dp_outcome,
    grades_outcome,
    merit_key,
    myp_outcome,
    national_outcome,
    percent_key,
    points_key,
    standard_outcome,
)


@dataclass(frozen=True)
class Rulebook:
    key: str
    label: str
    outcome: Callable
    rank_key: Callable
    pass_marks: bool  # a paper below its pass mark fails and prints the scale's failing grade
    combine_papers: bool  # two papers of one subject graded together
    fourth_subject: bool  # the national 4th-subject rule
    ranks_by_default: bool
    has_gpa: bool
    has_result: bool  # an overall PASS / FAIL
    # Who awards the official result this rulebook imitates. A school's own exam graded this
    # way is still the school's assessment, and every card says so.
    official_body: str = ""

    def show_rank(self, exam):
        return self.ranks_by_default if exam.show_rank is None else exam.show_rank

    @property
    def notice(self):
        if not self.official_body:
            return ""
        return f"A school assessment, not an official result. Official results are issued only by {self.official_body}."


def _total_key(row):
    from decimal import Decimal

    return (Decimal(row["total"]),)


RULEBOOKS = {
    "own": Rulebook(
        key="own",
        label="School's own rules",
        outcome=standard_outcome,
        rank_key=_total_key,
        pass_marks=True,
        combine_papers=False,
        fourth_subject=False,
        ranks_by_default=True,
        has_gpa=True,
        has_result=True,
    ),
    "national": Rulebook(
        key="national",
        label="Bangladesh national curriculum",
        official_body="the education board",
        outcome=national_outcome,
        rank_key=merit_key,
        pass_marks=True,
        combine_papers=True,
        fourth_subject=True,
        ranks_by_default=True,
        has_gpa=True,
        has_result=True,
    ),
    "cambridge": Rulebook(
        key="cambridge",
        label="Cambridge grades (school assessment)",
        official_body="Cambridge International Education",
        outcome=grades_outcome,
        rank_key=percent_key,
        pass_marks=False,
        combine_papers=False,
        fourth_subject=False,
        ranks_by_default=False,
        has_gpa=False,
        has_result=False,
    ),
    "edexcel": Rulebook(
        key="edexcel",
        label="Pearson Edexcel grades (school assessment)",
        official_body="Pearson",
        outcome=grades_outcome,
        rank_key=percent_key,
        pass_marks=False,
        combine_papers=False,
        fourth_subject=False,
        ranks_by_default=False,
        has_gpa=False,
        has_result=False,
    ),
    "ib_dp": Rulebook(
        key="ib_dp",
        label="IB Diploma Programme (school estimate)",
        official_body="the International Baccalaureate",
        outcome=dp_outcome,
        rank_key=points_key,
        pass_marks=False,
        combine_papers=False,
        fourth_subject=False,
        ranks_by_default=False,
        has_gpa=False,
        has_result=False,
    ),
    "ib_myp": Rulebook(
        key="ib_myp",
        label="IB Middle Years Programme (school assessment)",
        official_body="the International Baccalaureate",
        outcome=myp_outcome,
        rank_key=points_key,
        pass_marks=False,
        combine_papers=False,
        fourth_subject=False,
        ranks_by_default=False,
        has_gpa=False,
        has_result=False,
    ),
}


def rulebook(key):
    """
    The rulebook for a stored key.

    An unknown key is refused rather than quietly treated as another rulebook: calculating a
    programme's results by the wrong rules is worse than not calculating them.
    """
    from django.core.exceptions import ValidationError

    if key not in RULEBOOKS:
        raise ValidationError(f"Results cannot be worked out: the '{key}' rulebook is not supported.")
    return RULEBOOKS[key]


def official_notice(row):
    """The card's statement that it is the school's assessment, for rows old and new."""
    book = RULEBOOKS.get(row.get("system") or "")
    return book.notice if book else ""
