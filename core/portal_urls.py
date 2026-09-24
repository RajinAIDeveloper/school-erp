from django.urls import path

from . import portal_views

app_name = "portal"
urlpatterns = [
    path("privacy/", portal_views.privacy, name="privacy"),
    path("", portal_views.index, name="index"),
    path("attendance/", portal_views.attendance, name="attendance"),
    path("fees/", portal_views.fees, name="fees"),
    path("results/", portal_views.results, name="results"),
    path("progress/", portal_views.progress, name="progress"),
    path("transcripts/", portal_views.transcripts, name="transcripts"),
    path("transcripts/<int:pk>/pdf/", portal_views.transcript_pdf, name="transcript_pdf"),
]
