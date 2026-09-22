from django.contrib import admin

from .models import AcademicYear, ClassLevel, Section, Subject, SubjectTeacher, Term

admin.site.register([AcademicYear, Term, ClassLevel, Section, Subject, SubjectTeacher])
