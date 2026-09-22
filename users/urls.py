from django.urls import path

from . import views

app_name = "users"
urlpatterns = [
    path("", views.UserListView.as_view(), name="list"),
    path("new/", views.UserCreateView.as_view(), name="create"),
    path("<int:pk>/edit/", views.UserUpdateView.as_view(), name="update"),
    path("profile/", views.profile, name="profile"),
]
