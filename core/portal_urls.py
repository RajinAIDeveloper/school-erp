from django.urls import path
from . import portal_views
app_name="portal"
urlpatterns=[path("",portal_views.index,name="index"),path("attendance/",portal_views.attendance,name="attendance"),
            path("fees/",portal_views.fees,name="fees"),path("results/",portal_views.results,name="results")]
