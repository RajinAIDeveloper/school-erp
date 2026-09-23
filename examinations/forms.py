"""
Exam configuration forms.

Publication is the point of no return for an exam: once results are out, the papers,
their marks and their grading must describe what the students actually sat. These forms
are where that rule is enforced for the CRUD screens; `examinations.services` enforces
the same rule for mark writes.
"""

from django.core.exceptions import ValidationError

from core.forms import SchoolModelForm

from .models import Exam, ExamSchedule, Mark


class ExamForm(SchoolModelForm):
    class Meta:
        model = Exam
        fields = [
            "academic_year",
            "term",
            "name",
            "start_date",
            "end_date",
            "grade_scale",
            "assessment_system",
            "show_rank",
        ]

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            locked = Exam.objects.filter(pk=self.instance.pk, publication_version__gt=0).exists()
            if locked:
                raise ValidationError("Published exam configuration is locked.")
        term = cleaned.get("term")
        year = cleaned.get("academic_year")
        if term and year and term.academic_year_id != year.pk:
            self.add_error("term", "Select a term of the chosen academic year.")
        return cleaned


class ExamScheduleForm(SchoolModelForm):
    class Meta:
        model = ExamSchedule
        fields = [
            "exam",
            "class_level",
            "subject",
            "date",
            "start_time",
            "end_time",
            "full_marks",
            "pass_marks",
            "room",
            "grade_scale",
            "max_grade",
        ]

    def clean(self):
        cleaned = super().clean()
        exam_ids = [e.pk for e in [cleaned.get("exam")] if e]
        if self.instance.pk:
            exam_ids.append(ExamSchedule.objects.get(pk=self.instance.pk).exam_id)
        if exam_ids and Exam.objects.filter(pk__in=exam_ids, publication_version__gt=0).exists():
            raise ValidationError("Schedules of published exams cannot be changed.")
        if self.instance.pk:
            before = ExamSchedule.objects.get(pk=self.instance.pk)
            has_marks = Mark.objects.filter(schedule=before).exists()
            full = cleaned.get("full_marks")
            if has_marks and full is not None and full != before.full_marks:
                self.add_error(
                    "full_marks", "Marks have been entered out of the current full marks, so they are fixed."
                )
            from .parts import plain

            parts = list(before.components.all())
            if parts and full is not None and all(p.weight is None for p in parts):
                total = sum(p.full_marks for p in parts)
                if total != full:
                    self.add_error(
                        "full_marks", f"This paper's parts add up to {plain(total)}; change the parts first."
                    )
        exam, max_grade = cleaned.get("exam"), (cleaned.get("max_grade") or "").strip()
        if max_grade and exam:
            from .grading import scale_rules

            scale = cleaned.get("grade_scale") or exam.grade_scale
            letters = [r["letter"] for r in (scale_rules(scale) if scale else [])]
            if max_grade not in letters:
                self.add_error(
                    "max_grade", f"Choose a grade from the paper's scale: {', '.join(letters) or 'none set'}."
                )
        subject, class_level = cleaned.get("subject"), cleaned.get("class_level")
        if subject and class_level and subject.class_levels.exists():
            if not subject.class_levels.filter(pk=class_level.pk).exists():
                self.add_error("subject", f"{subject} is not taught in {class_level}.")
        return cleaned
