from django.urls import path

from core.crud import crud

from . import views
from .forms import DownloadForm, NoticeForm
from .models import DownloadCategory, DownloadItem, Notice

app_name = "downloads"
urlpatterns = [
    path("", views.listing, name="list"),
    path("new/", views.create, name="create"),
    path("<int:pk>/file/", views.download, name="file"),
    path("notices/", views.notices, name="notices"),
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
urlpatterns += crud(
    Notice,
    "downloads",
    "notice",
    NoticeForm.Meta.fields,
    [
        ("Title", "title"),
        ("Audience", "get_audience_display"),
        ("Published", "publish_at", "date"),
        ("Expires", "expires_at", "date"),
        ("Pinned", "is_pinned", "bool"),
    ],
    form=NoticeForm,
    search=("title", "body"),
    list_permission="downloads.change_notice",
    actions=(("Noticeboard", "downloads:notices", "downloads.view_downloaditem"),),
)
