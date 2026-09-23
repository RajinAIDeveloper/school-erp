from django.urls import path

from . import views

app_name = "users"
urlpatterns = [
    path("", views.UserListView.as_view(), name="list"),
    path("new/", views.UserCreateView.as_view(), name="create"),
    path("profile/", views.profile, name="profile"),
    path("provision/", views.provision_bulk, name="provision_bulk"),
    path("provision/<str:kind>/<int:pk>/", views.provision, name="provision"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.UserUpdateView.as_view(), name="update"),
    path("<int:pk>/reset/", views.reset, name="reset"),
]
