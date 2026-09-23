from django.urls import path

from . import views

app_name = "academics"
urlpatterns = [
    path("sections.json", views.sections_json, name="sections_json"),
    path("subject-plan/", views.subject_plan, name="subject_plan"),
]
