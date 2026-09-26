from django.urls import path

from . import views

app_name = "academics"
urlpatterns = [
    path("sections.json", views.sections_json, name="sections_json"),
    path("subjects/", views.subjects, name="subjects"),
    path("subjects/<int:pk>/", views.subject_detail, name="subject_detail"),
    path("subject-plan/", views.subject_plan, name="subject_plan"),
    path("subject-plan/<int:pk>/setup/", views.subject_setup, name="subject_setup"),
    path("teaching-plan/<int:pk>/edit/", views.teaching_plan_edit, name="teaching_plan_edit"),
]
