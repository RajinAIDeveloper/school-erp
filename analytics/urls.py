from django.urls import path

from . import views

app_name = "analytics"
urlpatterns = [
    path("student/<int:pk>/", views.student, name="student"),
]
