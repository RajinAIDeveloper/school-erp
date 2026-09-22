from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError,PermissionDenied
from django.shortcuts import get_object_or_404,render,redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from core.access import require_permission,sections_for,students_for,is_manager
from core.forms import TailwindFormMixin,SchoolModelForm
from core.exports import spreadsheet,pdf_response
from core.generic import ERPListView
from students.models import Enrollment,Student
from academics.models import ClassLevel,Section
from .models import Exam,ExamSchedule,Mark,GradeScale,GradeRule,ResultSnapshot,UnlockRequest
from .services import save_mark,build_result_sheet,publish_exam,assert_can_mark,active_unlock,review_unlock

class ExamListView(ERPListView):
    model=Exam
    permission_required="examinations.view_exam"
    page_title="Exams and results"
    columns=(("Exam","name"),("Year","academic_year"),("Start","start_date"),("Status","status","badge"))
    create_url_name="examinations:exam_create"
    update_url_name="examinations:exam_update"
    detail_url_name="examinations:exam_detail"
    extra_actions=(("Schedules","examinations:schedule_list"),("Mark entry","examinations:marks"),("Results","examinations:results"),("Grading","examinations:scale_list"),("Unlock requests","examinations:unlocks"))

@require_permission("examinations.view_exam")
def exam_detail(request,pk):
    exam=get_object_or_404(Exam,school=request.school,pk=pk)
    return render(request,"examinations/exam.html",{"exam":exam,"page_title":str(exam),"can_publish":is_manager(request.user)})

@require_permission("examinations.change_exam")
@require_POST
def publish(request,pk):
    exam=get_object_or_404(Exam,school=request.school,pk=pk)
    try:
        publish_exam(exam,request.user)
        messages.success(request,"Results published and snapshotted. Marks are now locked.")
    except ValidationError as e: messages.error(request," ".join(e.messages))
    return redirect("examinations:exam_detail",pk=pk)

class MarkFilter(TailwindFormMixin,forms.Form):
    schedule=forms.ModelChoiceField(queryset=None)
    section=forms.ModelChoiceField(queryset=None)
    def __init__(self,*args,user,school,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["schedule"].queryset=ExamSchedule.objects.filter(school=school).select_related("exam","class_level","subject")
        self.fields["section"].queryset=sections_for(user,school)
    def clean(self):
        d=super().clean()
        if d.get("schedule") and d.get("section") and d["schedule"].class_level_id!=d["section"].class_level_id:
            raise forms.ValidationError("Select a section belonging to this exam paper's class.")
        return d

class MarkForm(TailwindFormMixin,forms.Form):
    score=forms.DecimalField(max_digits=6,decimal_places=2,required=False)
    absent=forms.BooleanField(required=False)
    expected_version=forms.IntegerField(min_value=0,widget=forms.HiddenInput)

@require_permission("examinations.view_mark")
def marks(request):
    selector=MarkFilter(request.GET or None,user=request.user,school=request.school)
    rows=[]
    schedule=section=None
    locked=False
    if selector.is_bound and selector.is_valid():
        schedule=selector.cleaned_data["schedule"]
        section=selector.cleaned_data["section"]
        assert_can_mark(request.user,schedule)
        locked=schedule.exam.status=="published" and not active_unlock(request.user,schedule)
        for e in Enrollment.objects.filter(school=request.school,academic_year=schedule.exam.academic_year,section=section).select_related("student"):
            m=Mark.objects.filter(schedule=schedule,enrollment=e).first()
            rows.append({"enrollment":e,"form":MarkForm(initial={"score":m.marks_obtained if m else None,"absent":m.is_absent if m else False,"expected_version":m.version if m else 0})})
    return render(request,"examinations/marks.html",{"selector":selector,"rows":rows,"schedule":schedule,"section":section,"locked":locked,"page_title":"Mark entry"})

@require_permission("examinations.change_mark")
@require_POST
def mark_save(request):
    selector=MarkFilter(request.POST,user=request.user,school=request.school)
    form=MarkForm(request.POST)
    if selector.is_valid() and form.is_valid():
        schedule=selector.cleaned_data["schedule"]
        section=selector.cleaned_data["section"]
        try:
            enrollment_pk = int(request.POST.get("enrollment", ""))
        except (TypeError, ValueError):
            from django.http import HttpResponseBadRequest
            return HttpResponseBadRequest("Invalid enrollment.")
        enrollment=get_object_or_404(Enrollment,pk=enrollment_pk,school=request.school,
                                      academic_year=schedule.exam.academic_year,section=section)
        try:
            save_mark(user=request.user,schedule=schedule,enrollment=enrollment,**form.cleaned_data)
            messages.success(request,f"Saved marks for {enrollment.student.full_name}.")
        except ValidationError as e: messages.error(request," ".join(e.messages))
    else:
        messages.error(request,"Invalid mark: "+selector.errors.as_text()+" "+form.errors.as_text())
    from django.urls import reverse
    from urllib.parse import urlencode
    return redirect(reverse("examinations:marks")+"?"+urlencode({"schedule":request.POST.get("schedule",""),"section":request.POST.get("section","")}))

class ResultsFilter(TailwindFormMixin,forms.Form):
    exam=forms.ModelChoiceField(queryset=None)
    class_level=forms.ModelChoiceField(queryset=None)
    section=forms.ModelChoiceField(queryset=None,required=False,help_text="Leave blank for a class-wide sheet.")
    def __init__(self,*args,user,school,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields["exam"].queryset=Exam.objects.filter(school=school)
        self.fields["class_level"].queryset=ClassLevel.objects.filter(school=school)
        self.fields["section"].queryset=sections_for(user,school)
        if not is_manager(user): self.fields["section"].required=True
    def clean(self):
        d=super().clean()
        if d.get("section") and d.get("class_level") and d["section"].class_level_id!=d["class_level"].pk:
            raise forms.ValidationError("Section does not belong to this class.")
        return d

@require_permission("examinations.view_mark")
def results(request):
    form=ResultsFilter(request.GET or None,user=request.user,school=request.school)
    sheet=None
    if form.is_bound and form.is_valid():
        sheet=build_result_sheet(**form.cleaned_data)
        fmt=request.GET.get("format")
        if fmt in ("csv","xlsx","pdf"):
            subjects=[c["subject"] for c in sheet["rows"][0]["cells"]] if sheet["rows"] else []
            headers=["Class rank","Section rank","Section","Roll","Student",*subjects,"Total","Percent","GPA","Result"]
            rows=[[r["grade_rank"],r["rank"],r["section"],r["roll"],r["student"],
                   *["ABS" if c["absent"] else (c["score"] if not c["missing"] else "") for c in r["cells"]],
                   r["total"],r["percent"],r["gpa"],r["result"]] for r in sheet["rows"]]
            if fmt=="pdf": return pdf_response("Result sheet",headers,rows,request.school.name)
            stats=sheet["subject_stats"]
            return spreadsheet("results",headers,rows,fmt,extra_sheets=[("Subject analysis",["Subject","Entered","Absent","Missing","Average","Highest","Lowest","Failed","Pass percent"],
                 [[s[k] for k in ("subject","entered","absent","missing","average","highest","lowest","failed","pass_pct")] for s in stats])])
    return render(request,"examinations/results.html",{"form":form,"sheet":sheet,"page_title":"Results and subject analysis"})

@require_permission(None)
def report_card(request,exam_pk,student_pk):
    exam=get_object_or_404(Exam,pk=exam_pk,school=request.school)
    student=get_object_or_404(students_for(request.user,request.school),pk=student_pk)
    if not request.user.has_perm("examinations.view_mark") and exam.status!="published":
        raise PermissionDenied
    enr=get_object_or_404(Enrollment,student=student,academic_year=exam.academic_year)
    sheet=build_result_sheet(exam,enr.class_level,enr.section)
    row=next((r for r in sheet["rows"] if r["enrollment_id"]==enr.pk),None)
    if row is None:
        from django.http import Http404
        raise Http404
    snap=ResultSnapshot.objects.filter(exam=exam,enrollment=enr,version=exam.publication_version).first()
    if request.GET.get("format")=="pdf":
        data=[[c["subject"],c["full_marks"],"ABS" if c["absent"] else c["score"],c["letter"] if not c["missing"] else "?"] for c in row["cells"]]
        data.append(["Total",row["full_total"],row["total"],row["result"]])
        title=f"{exam.name} · {student.full_name}"+(" · DRAFT" if exam.status!="published" else "")
        subtitle=f"{request.school.name} | {enr.section} | GPA {row['gpa']} | Rank {row['rank']} | Version {exam.publication_version}"
        if snap: subtitle+=f" | Verification: {snap.verification_code}"
        return pdf_response(title,["Subject","Full marks","Obtained","Grade"],data,subtitle)
    return render(request,"examinations/report_card.html",{"row":row,"exam":exam,"snapshot":snap,"page_title":"Report card"})

def verify(request,code):
    snap=get_object_or_404(ResultSnapshot.objects.select_related("exam","school"),verification_code=code)
    # Public verification deliberately reveals no child's identity or marks.
    state="Current" if snap.exam.status=="published" and snap.version==snap.exam.publication_version else "Superseded or unpublished"
    from django.http import HttpResponse
    from django.utils.html import format_html
    return HttpResponse(format_html("<h1>Report verification</h1><p>{}</p><p>Version {} · {}</p>",snap.school.name,snap.version,state))

@require_permission("examinations.view_mark")
def unlocks(request):
    qs=UnlockRequest.objects.filter(school=request.school).select_related("schedule","requested_by")
    if not is_manager(request.user): qs=qs.filter(requested_by=request.user)
    return render(request,"examinations/unlocks.html",{"rows":qs,"can_review":is_manager(request.user),"page_title":"Mark unlock requests"})

@require_permission("examinations.change_mark")
@require_POST
def request_unlock(request,pk):
    schedule=get_object_or_404(ExamSchedule,school=request.school,pk=pk)
    assert_can_mark(request.user,schedule)
    reason=request.POST.get("reason","").strip()
    if not reason: messages.error(request,"A correction reason is required.")
    elif schedule.exam.status!="published": messages.error(request,"This exam is not locked.")
    else:
        UnlockRequest.objects.create(school=request.school,schedule=schedule,requested_by=request.user,reason=reason)
        messages.success(request,"Unlock requested for this subject and class.")
    return redirect("examinations:unlocks")

@require_permission("examinations.change_exam")
@require_POST
def unlock_review(request,pk):
    obj=get_object_or_404(UnlockRequest,school=request.school,pk=pk)
    try:
        review_unlock(obj,request.user,request.POST.get("decision")=="approve")
        messages.success(request,"Request reviewed.")
    except ValidationError as e: messages.error(request," ".join(e.messages))
    return redirect("examinations:unlocks")

@require_permission("examinations.change_gradescale")
def grade_rules(request,pk):
    from django.forms import inlineformset_factory, BaseInlineFormSet
    class RuleFormSet(BaseInlineFormSet):
        def clean(self):
            super().clean()
            if any(self.errors):
                return
            ranges = sorted((f.cleaned_data["min_percent"], f.cleaned_data["max_percent"])
                            for f in self.forms if f.cleaned_data and not f.cleaned_data.get("DELETE"))
            if not ranges or ranges[0][0] != 0 or ranges[-1][1] != 100:
                raise forms.ValidationError("Grading rules must cover 0 through 100 percent.")
            for previous, current in zip(ranges, ranges[1:]):
                if current[0] <= previous[1] or current[0] - previous[1] > Decimal("0.01"):
                    raise forms.ValidationError("Grade ranges cannot overlap or leave gaps.")
    scale=get_object_or_404(GradeScale,school=request.school,pk=pk)
    FormSet=inlineformset_factory(GradeScale,GradeRule,fields=["letter","min_percent","max_percent","grade_point"],extra=1,can_delete=True,formset=RuleFormSet)
    formset=FormSet(request.POST or None,instance=scale)
    if request.method=="POST" and formset.is_valid():
        formset.save()
        messages.success(request,"Grading rules saved. Published snapshots are unchanged.")
        return redirect("examinations:rules",pk=pk)
    return render(request,"examinations/rules.html",{"formset":formset,"page_title":"Grading rules: "+scale.name})
