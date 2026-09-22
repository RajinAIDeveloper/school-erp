from django.urls import path
from core.crud import crud
from . import views
from .models import Exam,ExamSchedule,GradeScale
app_name="examinations"
urlpatterns=[
    path("",views.ExamListView.as_view(),name="exam_list"),
    path("<int:pk>/",views.exam_detail,name="exam_detail"),
    path("<int:pk>/publish/",views.publish,name="publish"),
    path("marks/",views.marks,name="marks"),
    path("marks/save/",views.mark_save,name="save_mark"),
    path("results/",views.results,name="results"),
    path("<int:exam_pk>/report/<int:student_pk>/",views.report_card,name="report_card"),
    path("verify/<uuid:code>/",views.verify,name="verify"),
    path("unlocks/",views.unlocks,name="unlocks"),
    path("schedule/<int:pk>/unlock/",views.request_unlock,name="request_unlock"),
    path("unlocks/<int:pk>/review/",views.unlock_review,name="unlock_review"),
    path("scales/<int:pk>/rules/",views.grade_rules,name="rules"),
]
urlpatterns+=crud(Exam,"examinations","exam",["academic_year","name","start_date","end_date","grade_scale"],
    [("Exam","name"),("Year","academic_year"),("Status","status")],prefix="manage/")
urlpatterns+=crud(ExamSchedule,"examinations","schedule",["exam","class_level","subject","date","start_time","end_time","full_marks","pass_marks","room"],
    [("Exam","exam"),("Class","class_level"),("Subject","subject"),("Full marks","full_marks"),("Pass marks","pass_marks")])
urlpatterns+=crud(GradeScale,"examinations","scale",["name","is_default"],[("Scale","name"),("Default","is_default","bool")])
# Each scale's rule editor is linked from this custom list.
