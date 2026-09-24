from django.urls import path

from core.crud import crud

from . import views
from .forms import ExamForm, ExamScheduleForm
from .models import Exam, ExamSchedule, ExamSeries, GradeScale

app_name = "examinations"
urlpatterns = [
    path("", views.ExamListView.as_view(), name="exam_list"),
    path("<int:pk>/", views.exam_detail, name="exam_detail"),
    path("<int:pk>/publish/", views.publish, name="publish"),
    path("marks/", views.marks, name="marks"),
    path("marks/save/", views.mark_save, name="save_mark"),
    path("marks/sheet.pdf", views.mark_sheet, name="mark_sheet"),
    path("marks/import/", views.marks_import, name="marks_import"),
    path("<int:pk>/seating/", views.seat_plans, name="seat_plans"),
    path("<int:pk>/seating/sitting/", views.seat_plan, name="seat_plan"),
    path("results/", views.results, name="results"),
    path("admit-cards.pdf", views.admit_cards, name="admit_cards"),
    path("report-cards.pdf", views.report_cards, name="report_cards"),
    path("<int:pk>/routine/", views.exam_routine, name="exam_routine"),
    path("progress/<int:student_pk>/", views.progress, name="progress"),
    path("<int:exam_pk>/report/<int:student_pk>/", views.report_card, name="report_card"),
    path("verify/<uuid:code>/", views.verify, name="verify"),
    path("unlocks/", views.unlocks, name="unlocks"),
    path("schedule/<int:pk>/unlock/", views.request_unlock, name="request_unlock"),
    path("schedule/<int:pk>/parts/", views.paper_parts, name="paper_parts"),
    path("comments/", views.comments, name="comments"),
    path("estimates/", views.forecasts, name="forecasts"),
    path("combined/", views.combined_list, name="combined_list"),
    path("combined/<int:pk>/", views.combined_detail, name="combined_detail"),
    path("combined/<int:pk>/card/<int:student_pk>/", views.combined_card, name="combined_card"),
    path("combined/verify/<uuid:code>/", views.combined_verify, name="combined_verify"),
    path("series/", views.series_list, name="series_list"),
    path("series/<int:pk>/", views.series_detail, name="series_detail"),
    path("series/<int:pk>/results/", views.series_results, name="series_results"),
    path("series/<int:pk>/entries.csv", views.series_export, name="series_export"),
    path("registration/", views.board_registration, name="board_registration"),
    path("unlocks/<int:pk>/review/", views.unlock_review, name="unlock_review"),
    path("scales/<int:pk>/rules/", views.grade_rules, name="rules"),
    path("scales/presets/", views.scale_presets, name="scale_presets"),
]
urlpatterns += crud(
    Exam,
    "examinations",
    "exam",
    ExamForm.Meta.fields,
    [("Exam", "name"), ("Year", "academic_year"), ("Term", "term"), ("Status", "status", "badge")],
    prefix="manage/",
    form=ExamForm,
    search=("name",),
)
urlpatterns += crud(
    ExamSchedule,
    "examinations",
    "schedule",
    ExamScheduleForm.Meta.fields,
    [
        ("Exam", "exam"),
        ("Class", "class_level"),
        ("Subject", "subject"),
        ("Date", "date", "date"),
        ("Full marks", "full_marks"),
        ("Pass marks", "pass_marks"),
    ],
    form=ExamScheduleForm,
)
urlpatterns += crud(
    GradeScale,
    "examinations",
    "scale",
    ["name", "is_default"],
    [("Scale", "name"), ("Default", "is_default", "bool")],
    actions=(("Add a programme's scale", "examinations:scale_presets", "examinations.add_gradescale"),),
)
# Each scale's rule editor is linked from this custom list.
urlpatterns += crud(
    ExamSeries,
    "examinations",
    "series",
    ["body", "name", "centre_number", "entry_deadline", "results_date", "notes"],
    [("Series", "__str__"), ("Centre", "centre_number"), ("Results", "results_date", "date")],
    prefix="series/manage/",
    actions=(("Series pages", "examinations:series_list", "examinations.view_examseries"),),
)
