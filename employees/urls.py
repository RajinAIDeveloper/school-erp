from django.urls import path

from . import views

app_name = "employees"
urlpatterns = [
    path("", views.EmployeeListView.as_view(), name="list"),
    path("new/", views.EmployeeCreateView.as_view(), name="create"),
    path("me/", views.me, name="me"),
    path("export/", views.export, name="export"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.EmployeeUpdateView.as_view(), name="update"),
    path("<int:pk>/documents/new/", views.document_upload, name="document_upload"),
    path("documents/<int:pk>/", views.document_download, name="document"),
]
