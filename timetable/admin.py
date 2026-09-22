from django.contrib import admin

from .models import Period, Room, RoutineSlot

admin.site.register([Period, Room, RoutineSlot])
