from django.contrib import admin

from core.admin_base import ReadOnlyAdmin

from .models import Account, JournalEntry, JournalLine, Payroll

admin.site.register(Account)
admin.site.register([JournalEntry, JournalLine, Payroll], ReadOnlyAdmin)
