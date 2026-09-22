from django.contrib import admin

from core.admin_base import ReadOnlyAdmin

from .models import Exam, ExamSchedule, GradeRule, GradeScale, Mark, ResultSnapshot, UnlockRequest

admin.site.register([Exam, ExamSchedule, Mark, ResultSnapshot, UnlockRequest], ReadOnlyAdmin)
admin.site.register([GradeScale, GradeRule])
