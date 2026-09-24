"""A deleted page or worksheet takes its file with it, once the deletion is committed."""

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import SubmissionFile, TaskResource


@receiver(post_delete, sender=SubmissionFile)
@receiver(post_delete, sender=TaskResource)
def remove_stored_file(sender, instance, **kwargs):
    field = instance.file
    if field and field.name:
        storage, name = field.storage, field.name
        transaction.on_commit(lambda: storage.delete(name))
