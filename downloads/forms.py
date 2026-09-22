from core.forms import SchoolModelForm

from .models import DownloadItem


class DownloadForm(SchoolModelForm):
    class Meta:
        model = DownloadItem
        fields = ["category", "title", "description", "file", "audience", "class_levels", "is_active"]
