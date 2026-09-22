from django.contrib import admin
from core.admin_base import ReadOnlyAdmin
from .models import FeeCategory,FeeConcession,FeeInvoice,FeeInvoiceItem,FeePayment,FeeStructure
admin.site.register([FeeCategory,FeeConcession,FeeStructure])
admin.site.register([FeeInvoice,FeeInvoiceItem,FeePayment],ReadOnlyAdmin)
