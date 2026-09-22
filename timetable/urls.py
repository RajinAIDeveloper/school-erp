from django.urls import path

from core.crud import crud

from . import views
from .models import Period, Room, RoutineSlot

app_name = "timetable"

ROUTINE_ACTIONS = (
    ("Routine", "timetable:routine", "timetable.view_routineslot"),
    ("Edit week", "timetable:grid_edit", "timetable.change_routineslot"),
    ("Utilisation", "timetable:utilisation", "timetable.view_routineslot"),
)

urlpatterns = [
    path("", views.routine, name="routine"),
    path("edit/", views.grid_edit, name="grid_edit"),
    path("free-teachers.json", views.free_teachers_json, name="free_teachers"),
    path("utilisation/", views.utilisation, name="utilisation"),
]

for model, key, fields, cols in [
    (
        Period,
        "period",
        ["name", "order", "start_time", "end_time", "is_break"],
        [("Period", "name"), ("Start", "start_time"), ("End", "end_time"), ("Break", "is_break", "bool")],
    ),
    (Room, "room", ["name", "capacity"], [("Room", "name"), ("Capacity", "capacity")]),
    (
        RoutineSlot,
        "slot",
        ["academic_year", "section", "weekday", "period", "subject", "teacher", "room"],
        [
            ("Day", "get_weekday_display"),
            ("Period", "period"),
            ("Section", "section"),
            ("Subject", "subject"),
            ("Teacher", "teacher"),
            ("Room", "room"),
        ],
    ),
]:
    urlpatterns += crud(
        model,
        "timetable",
        key,
        fields,
        cols,
        actions=ROUTINE_ACTIONS,
        list_permission=f"timetable.change_{model._meta.model_name}",
    )
