from django.contrib import admin

from .models import DownloadCategory, DownloadItem

admin.site.register([DownloadCategory, DownloadItem])
