from django.contrib import admin
from .models import Student,Guardian,StudentGuardian,Enrollment,StudentDocument
@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display=("student_id","full_name","school","status")
    search_fields=("student_id","first_name","last_name")
admin.site.register([Guardian,StudentGuardian,Enrollment,StudentDocument])
