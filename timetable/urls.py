from django.urls import path
from core.crud import crud
from .models import Period,Room,RoutineSlot
from .views import routine
app_name="timetable"
urlpatterns=[path("",routine,name="routine")]
for model,key,fields,cols in [
    (Period,"period",["name","order","start_time","end_time","is_break"],[("Period","name"),("Start","start_time"),("End","end_time"),("Break","is_break","bool")]),
    (Room,"room",["name","capacity"],[("Room","name"),("Capacity","capacity")]),
    (RoutineSlot,"slot",["academic_year","section","weekday","period","subject","teacher","room"],[("Day","get_weekday_display"),("Period","period"),("Section","section"),("Subject","subject"),("Teacher","teacher"),("Room","room")]),
]:
    urlpatterns+=crud(model,"timetable",key,fields,cols,actions=(("Routine","timetable:routine"),))
