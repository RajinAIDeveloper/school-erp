from django.urls import path

from . import views

app_name = "analytics"
urlpatterns = [
    path("student/<int:pk>/", views.student, name="student"),
]
urlpatterns += [
    path("", views.home, name="home"),
    path("paper/", views.paper, name="paper"),
    path("section/", views.section, name="section"),
]
