from django.urls import path

from core.crud import crud

from .models import Holiday
from .views import calendar_export

app_name = "holidays"
urlpatterns = crud(
    Holiday,
    "holidays",
    "",
    ["name", "holiday_type", "start_date", "end_date", "description", "closes_school"],
    [
        ("Holiday", "name"),
        ("Type", "holiday_type"),
        ("From", "start_date"),
        ("Until", "end_date"),
        ("School closed", "closes_school", "bool"),
    ],
    prefix="",
    search=("name",),
    actions=(("Calendar export", "holidays:calendar", "holidays.view_holiday"),),
)
urlpatterns += [path("calendar.ics", calendar_export, name="calendar")]
