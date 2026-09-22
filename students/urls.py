from django.urls import path
from core.crud import crud
from . import views
from .models import Enrollment
from .forms import EnrollmentForm
app_name = "students"
urlpatterns = [
    path("", views.StudentListView.as_view(), name="list"),
    path("new/", views.StudentCreateView.as_view(), name="create"),
    path("<int:pk>/", views.detail, name="detail"),
    path("<int:pk>/edit/", views.StudentUpdateView.as_view(), name="update"),
    path("<int:student_pk>/guardian/", views.guardian, name="guardian_create"),
    path("<int:pk>/documents/new/", views.document_upload, name="document_upload"),
    path("documents/<int:pk>/", views.document_download, name="document"),
    path("<int:pk>/id-card.pdf", views.id_card, name="id_card"),
    path("promote/", views.promotion, name="promote"),
    path("import/", views.import_csv, name="import"),
    path("export/", views.export, name="export"),
]
urlpatterns += crud(Enrollment, "students", "enrollment", EnrollmentForm.Meta.fields,
    (("Student", "student"), ("Year", "academic_year"), ("Section", "section"), ("Roll", "roll_number"), ("Status", "status")), form=EnrollmentForm)
