from django.urls import path

from core.crud import crud

from . import views
from .forms import DownloadForm
from .models import DownloadCategory, DownloadItem

app_name = "downloads"
urlpatterns = [
    path("", views.listing, name="list"),
    path("new/", views.create, name="create"),
    path("<int:pk>/file/", views.download, name="file"),
]
urlpatterns += crud(DownloadCategory, "downloads", "category", ["name"], [("Category", "name")])
urlpatterns += crud(
    DownloadItem,
    "downloads",
    "item",
    DownloadForm.Meta.fields,
    [("Title", "title"), ("Category", "category"), ("Audience", "audience"), ("Active", "is_active", "bool")],
    form=DownloadForm,
    list_permission="downloads.change_downloaditem",
    actions=(
        ("Downloads", "downloads:list"),
        ("Categories", "downloads:category_list", "downloads.view_downloadcategory"),
    ),
)
