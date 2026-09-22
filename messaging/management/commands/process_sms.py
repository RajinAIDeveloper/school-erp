from django.core.management.base import BaseCommand
from django.db import transaction
from messaging.models import SMSMessage
from messaging.backends import get_backend

class Command(BaseCommand):
    help="Process queued SMS messages. Schedule this command in a worker; development uses the console backend."
    def add_arguments(self,parser):
        parser.add_argument("--limit",type=int,default=100)
    def handle(self,*args,**options):
        sent=failed=0
        backend=get_backend()
        ids=list(SMSMessage.objects.filter(status="queued").order_by("pk").values_list("pk",flat=True)[:max(0,options["limit"])])
        for pk in ids:
            with transaction.atomic():
                msg=SMSMessage.objects.select_for_update().filter(pk=pk,status="queued").first()
                if msg:
                    if backend.deliver(msg): sent+=1
                    else: failed+=1
        self.stdout.write(f"Sent {sent}; failed {failed}.")
