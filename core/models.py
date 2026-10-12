from django.conf import settings
from django.db import models

from core.attachments import PrivateAttachmentStorage

attachment_storage = PrivateAttachmentStorage()


class Announcement(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class DiscussionTopic(models.Model):
    title = models.CharField(max_length=200)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="discussion_topics")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class DiscussionPost(models.Model):
    topic = models.ForeignKey(DiscussionTopic, on_delete=models.CASCADE, related_name="posts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="discussion_posts")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author} - {self.topic}"


class Attachment(models.Model):
    """A member-uploaded file attached to an announcement or a discussion post.

    The blob lives under ``settings.PRIVATE_UPLOAD_ROOT`` (never MEDIA_ROOT) and is
    only ever streamed by the permission-checked ``attachment-serve`` view.
    """

    KIND_IMAGE = "image"
    KIND_DOCUMENT = "document"
    KIND_ARCHIVE = "archive"
    KIND_CHOICES = [(KIND_IMAGE, "图片"), (KIND_DOCUMENT, "文档"), (KIND_ARCHIVE, "压缩包")]

    announcement = models.ForeignKey(
        Announcement, on_delete=models.CASCADE, related_name="attachments", null=True, blank=True
    )
    post = models.ForeignKey(
        DiscussionPost, on_delete=models.CASCADE, related_name="attachments", null=True, blank=True
    )
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="attachments",
        null=True,
        blank=True,
    )
    file = models.FileField(upload_to="", storage=attachment_storage, max_length=255)
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_DOCUMENT)
    mime_type = models.CharField(max_length=128, default="application/octet-stream")
    size = models.PositiveBigIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(announcement__isnull=False, post__isnull=True)
                    | models.Q(announcement__isnull=True, post__isnull=False)
                ),
                name="attachment_exactly_one_parent",
            )
        ]

    def __str__(self):
        return self.original_name

    @property
    def parent(self):
        return self.announcement or self.post

    @property
    def is_image(self) -> bool:
        return self.kind == self.KIND_IMAGE

    def can_serve_inline(self) -> bool:
        """Only raster images may be shown inline; everything else is downloaded."""
        return self.is_image and self.mime_type.startswith("image/")
