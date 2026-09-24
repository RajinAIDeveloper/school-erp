"""
Helpers shared by the examination screens: verification links, card access, sheet cells.
"""

from django.shortcuts import get_object_or_404
from django.urls import reverse

from core.access import is_manager, may_use_section, students_for
from students.models import Enrollment


def verification_url(request, snapshot):
    """
    The address printed on a report card, in full.

    A code on its own is no use to the employer or college holding the card: they have to
    know where to type it. This is the whole link, so it can be read off paper.
    """
    if snapshot is None:
        return ""
    return request.build_absolute_uri(reverse("examinations:verify", args=[snapshot.verification_code]))


def _cell_text(row, schedule_id):
    """One paper's mark for a spreadsheet cell: the score, ABS, blank if not sat."""
    cell = next((c for c in row["cells"] if c["schedule_id"] == schedule_id), None)
    if cell is None:
        return ""
    if cell.get("exempt"):
        return "EX"
    if cell["absent"]:
        return "ABS"
    return cell["score"] if not cell["missing"] else ""


def card_enrollment(request, exam, student_pk):
    """
    The enrollment a report card is for, if this person may see it.

    One rule for every historical card, the same one bulk printing uses: staff are judged by
    what they taught in the exam's own year, families by whether the child is theirs. A
    teacher can reprint last year's card for the section they taught last year, and cannot
    print this year's card for a pupil they merely teach now in another subject.
    """
    from students.models import Student

    student = get_object_or_404(Student, school=request.school, pk=student_pk)
    enr = get_object_or_404(Enrollment, student=student, academic_year=exam.academic_year)
    user = request.user
    if is_manager(user):
        allowed = True
    elif hasattr(user, "student_profile") or hasattr(user, "guardian_profile"):
        allowed = students_for(user, request.school).filter(pk=student.pk).exists()
    elif user.has_perm("examinations.view_mark"):
        allowed = may_use_section(user, request.school, enr.section, exam.academic_year)
    else:
        allowed = students_for(user, request.school).filter(pk=student.pk).exists()
    if not allowed:
        from django.http import Http404

        raise Http404
    return student, enr


def mask_name(name):
    """Initials and the length of each word: enough to match a card, not enough to read it off."""
    return " ".join(word[0] + "•" * (len(word) - 1) for word in str(name).split() if word)


def _as_exam(combined):
    """What the report card needs to know about "the exam" when the result is a combination."""
    from types import SimpleNamespace

    return SimpleNamespace(
        pk=combined.pk,
        name=combined.name,
        academic_year=combined.academic_year,
        status=combined.status,
        end_date=None,
    )
