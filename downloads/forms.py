from core.forms import SchoolModelForm

from .models import DownloadItem, Notice


class DownloadForm(SchoolModelForm):
    class Meta:
        model = DownloadItem
        fields = ["category", "title", "description", "file", "audience", "class_levels", "is_active"]


class NoticeForm(SchoolModelForm):
    class Meta:
        model = Notice
        fields = ["title", "body", "audience", "class_levels", "publish_at", "expires_at", "is_pinned"]

    def clean(self):
        cleaned = super().clean()
        publish, expires = cleaned.get("publish_at"), cleaned.get("expires_at")
        if publish and expires and expires < publish:
            self.add_error("expires_at", "A notice cannot expire before it is published.")
        return cleaned
