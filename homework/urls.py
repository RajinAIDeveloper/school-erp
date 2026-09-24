from django.urls import path

from . import views

app_name = "homework"

urlpatterns = [
    path("", views.task_list, name="list"),
    path("new/", views.task_create, name="create"),
    path("<int:pk>/", views.task_detail, name="detail"),
    path("<int:pk>/edit/", views.task_edit, name="edit"),
    path("<int:pk>/withdraw/", views.task_withdraw, name="withdraw"),
    path("<int:pk>/delete/", views.task_delete, name="delete"),
    path("<int:pk>/check/<int:section>/", views.check, name="check"),
    path("submission/<int:pk>/", views.review, name="review"),
    path("file/<int:pk>/", views.hand_in_file, name="file"),
    path("resource/<int:pk>/", views.resource_file, name="resource"),
    path("limits/", views.limits, name="limits"),
    path("todo/", views.todo, name="todo"),
    path("todo/<int:pk>/", views.todo_task, name="todo_task"),
]
