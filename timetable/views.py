from django import forms
from django.shortcuts import render
from core.access import require_permission,sections_for,students_for,is_manager
from core.forms import TailwindFormMixin
from core.exports import spreadsheet,pdf_response
from .models import RoutineSlot
@require_permission("timetable.view_routineslot")
def routine(request):
    from academics.models import AcademicYear,Section
    from employees.models import Employee
    class Filter(TailwindFormMixin,forms.Form):
        academic_year=forms.ModelChoiceField(queryset=AcademicYear.objects.filter(school=request.school),required=False)
        section=forms.ModelChoiceField(queryset=Section.objects.filter(school=request.school),required=False)
        teacher=forms.ModelChoiceField(queryset=Employee.objects.filter(school=request.school,employee_type="teacher"),required=False)
    form=Filter(request.GET or None)
    qs=RoutineSlot.objects.filter(school=request.school).select_related("academic_year","section__class_level","teacher","subject","room","period")
    if not is_manager(request.user):
        from django.db.models import Q
        qs=qs.filter(Q(section__in=sections_for(request.user,request.school))|Q(section__enrollments__student__in=students_for(request.user,request.school))).distinct()
    if form.is_bound and form.is_valid():
        for name,value in form.cleaned_data.items():
            if value: qs=qs.filter(**{name:value})
    else:
        qs=qs.filter(academic_year__is_current=True)
    headers=["Year","Day","Period","Class / section","Subject","Teacher","Room"]
    rows=[[str(r.academic_year),r.get_weekday_display(),str(r.period),str(r.section),str(r.subject),str(r.teacher or ""),str(r.room or "")] for r in qs]
    fmt=request.GET.get("format")
    if fmt=="pdf": return pdf_response("Class routine",headers,rows,request.school.name)
    if fmt in ("csv","xlsx"): return spreadsheet("routine",headers,rows,fmt)
    return render(request,"timetable/routine.html",{"form":form,"headers":headers,"rows":rows,"page_title":"Class routine"})
