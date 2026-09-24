from django.urls import path

from . import public_views as views

app_name = "apply"

urlpatterns = [
    path("<slug:slug>/", views.landing, name="landing"),
    path("<slug:slug>/t/<str:token>/", views.open_link, name="open"),
    path("<slug:slug>/done/", views.done, name="done"),
    path("<slug:slug>/my/", views.mine, name="mine"),
    path("<slug:slug>/my/<int:pk>/slip/", views.slip, name="slip"),
    path("<slug:slug>/<int:pk>/", views.apply, name="form"),
]
