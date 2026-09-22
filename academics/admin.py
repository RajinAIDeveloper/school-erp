from django.contrib import admin
from .models import AcademicYear,Term,ClassLevel,Section,Subject,SubjectTeacher
admin.site.register([AcademicYear,Term,ClassLevel,Section,Subject,SubjectTeacher])
