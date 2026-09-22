from django.urls import path

from core.crud import crud

from . import views
from .models import SMSTemplate

app_name = "messaging"
urlpatterns = [
    path("", views.BatchListView.as_view(), name="batch_list"),
    path("outbox/", views.message_list, name="message_list"),
    path("compose/", views.compose, name="compose"),
    path("batch/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("batch/<int:pk>/retry/", views.retry_batch, name="retry_batch"),
    path("<int:pk>/retry/", views.retry, name="retry"),
    path("gateway-test/", views.gateway_test, name="gateway_test"),
]
urlpatterns += crud(
    SMSTemplate,
    "messaging",
    "template",
    ["name", "body"],
    [("Name", "name"), ("Message", "body")],
    search=("name", "body"),
    actions=(
        ("Batches", "messaging:batch_list", "messaging.view_smsmessage"),
        ("Compose", "messaging:compose", "messaging.add_smsmessage"),
    ),
)
