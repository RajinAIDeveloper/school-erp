"""
Who may see which analytics. Every analytics screen and query goes through `scope_for`.

- The platform administrator, and a school's Administrator, Principal and Vice Principal: the
  whole school.
- A class teacher: every subject of the section they are class teacher of.
- A subject teacher: each subject they teach, for the sections they teach it in that year.
  Teaching one paper of a subject examined in two (Bangla 1st Paper) counts for the subject.
- A student: themselves. A guardian: their own children.
- Anyone else: nothing.

Always within one school: nothing here ever reaches another school's results.
"""

from dataclasses import dataclass, field

from django.db.models import Q

from core.access import is_manager, students_for


@dataclass(frozen=True)
class Scope:
    school: object
    whole_school: bool = False
    # Sections seen in full: every subject and the overall result (class teacher).
    sections: frozenset = field(default_factory=frozenset)
    # (section id, subject id) pairs seen for that subject only (subject teacher).
    subjects: frozenset = field(default_factory=frozenset)
    # Students seen as a family sees them: their own results, with class averages but no names.
    students: frozenset = field(default_factory=frozenset)
    # The year a teacher's sections and subjects belong to: a section is reused every year, and
    # last year's Class 9 A is not this year's class teacher's business.
    year_id: int | None = None

    @property
    def family(self):
        return bool(self.students) and not (self.whole_school or self.sections or self.subjects)

    @property
    def empty(self):
        return not (self.whole_school or self.sections or self.subjects or self.students)

    def results(self, queryset):
        """Overall exam results (ExamResultFact) this scope may see."""
        queryset = queryset.filter(school=self.school)
        if self.whole_school:
            return queryset
        condition = Q(pk__in=[])
        if self.sections:
            condition |= Q(section_id__in=self.sections, academic_year_id=self.year_id)
        if self.students:
            condition |= Q(student_id__in=self.students)
        return queryset.filter(condition)

    def subject_results(self, queryset):
        """Subject results (SubjectResultFact) this scope may see."""
        queryset = queryset.filter(school=self.school)
        if self.whole_school:
            return queryset
        taught = Q(pk__in=[])
        if self.sections:
            taught |= Q(section_id__in=self.sections)
        for section_id, subject_id in self.subjects:
            taught |= Q(section_id=section_id, subject_id=subject_id)
        condition = taught & Q(exam__academic_year_id=self.year_id)
        if self.students:
            condition |= Q(student_id__in=self.students)
        return queryset.filter(condition)

    def sees_subject(self, section_id, subject_id):
        return self.whole_school or section_id in self.sections or (section_id, subject_id) in self.subjects


def scope_for(user, school, academic_year=None):
    """What `user` may analyse in `school`, for one academic year (the current one by default)."""
    from academics.models import AcademicYear, Section, SubjectTeacher

    if school is None or not user.is_authenticated or not user.is_active:
        return Scope(school=school)
    if not user.is_superuser and user.school_id != school.pk:
        return Scope(school=school)
    if user.is_superuser or is_manager(user):
        return Scope(school=school, whole_school=True)

    year = academic_year or AcademicYear.current_for(school)
    sections, subjects, students = set(), set(), set()
    employee = getattr(user, "employee_profile", None)
    if employee is not None:
        # Class teacher is a standing role with no year of its own, so it speaks for now.
        sections = set(Section.objects.filter(school=school, class_teacher=employee).values_list("pk", flat=True))
        if year is not None:
            for section_id, subject_id, unit_id in SubjectTeacher.objects.filter(
                school=school, teacher=employee, academic_year=year
            ).values_list("section_id", "subject_id", "subject__combines_into_id"):
                subjects.add((section_id, subject_id))
                if unit_id:
                    subjects.add((section_id, unit_id))
    if hasattr(user, "student_profile") or hasattr(user, "guardian_profile"):
        students = set(students_for(user, school).values_list("pk", flat=True))
    return Scope(
        school=school,
        sections=frozenset(sections),
        subjects=frozenset(subjects),
        students=frozenset(students),
        year_id=year.pk if year else None,
    )
