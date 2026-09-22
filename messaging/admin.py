from django.contrib import admin

from .models import SMSBatch, SMSMessage, SMSTemplate

admin.site.register([SMSTemplate, SMSBatch, SMSMessage])
