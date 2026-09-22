from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group, Permission
from core.roles import ALL_ROLES,FULL_APP_ACCESS,EXPLICIT_PERMS
class Command(BaseCommand):
    help = "Create/update the ERP roles and their model permissions."
    def handle(self,*args,**options):
        for name in ALL_ROLES:
            group,_ = Group.objects.get_or_create(name=name)
            ids = set(Permission.objects.filter(content_type__app_label__in=FULL_APP_ACCESS.get(name,[])).values_list("id",flat=True))
            for app,codename in EXPLICIT_PERMS.get(name,[]):
                ids.update(Permission.objects.filter(content_type__app_label=app,codename=codename).values_list("id",flat=True))
            # The ERP administration pages replace direct Django-admin access.
            group.permissions.set(ids)
        self.stdout.write(self.style.SUCCESS("Configured 7 ERP roles."))
