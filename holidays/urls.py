from django.urls import path

from core.crud import crud

from . import views
from .models import Holiday

app_name = "holidays"

urlpatterns = crud(
    Holiday,
    "holidays",
    "",
    ["name", "holiday_type", "start_date", "end_date", "description", "closes_school"],
    [
        ("Holiday", "name"),
        ("Type", "get_holiday_type_display"),
        ("From", "start_date", "date"),
        ("Until", "end_date", "date"),
        ("Days", "days"),
        ("School closed", "closes_school", "bool"),
    ],
    prefix="",
    search=("name",),
    filters=(("holiday_type", "Type", Holiday.Type.choices),),
    actions=(
        ("Calendar", "holidays:month", "holidays.view_holiday"),
        ("Export iCalendar", "holidays:calendar", "holidays.view_holiday"),
    ),
)

urlpatterns += [
    path("calendar/", views.month_view, name="month"),
    path("calendar.ics", views.calendar_export, name="calendar"),
    path("import-national/", views.import_national, name="import_national"),
]
