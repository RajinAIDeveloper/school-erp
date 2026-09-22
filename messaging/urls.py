from django.urls import path

from core.crud import crud

from . import views
from .models import SMSTemplate

app_name = "messaging"
urlpatterns = [
    path("", views.listing, name="batch_list"),
    path("compose/", views.compose, name="compose"),
    path("<int:pk>/retry/", views.retry, name="retry"),
]
urlpatterns += crud(SMSTemplate, "messaging", "template", ["name", "body"], [("Name", "name"), ("Body", "body")])
