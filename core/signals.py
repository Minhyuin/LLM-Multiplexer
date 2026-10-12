from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from core.attachments import delete_storage_file
from core.models import Attachment


@receiver(post_delete, sender=Attachment)
def _remove_attachment_blob(sender, instance, **kwargs):
    """Delete the stored blob once its Attachment row is really gone.

    ``post_delete`` also fires for rows removed by a cascade (announcement/post/
    topic deletion).  The blob is only removed *after the transaction commits*:
    if the surrounding transaction rolls back, the row comes back and the file
    must still be downloadable.
    """
    storage = instance.file.storage
    name = instance.file.name
    if not name:
        return
    transaction.on_commit(lambda: delete_storage_file(storage, name))
