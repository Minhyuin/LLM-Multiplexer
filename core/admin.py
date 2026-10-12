from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from core.models import Announcement, Attachment, DiscussionPost, DiscussionTopic


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0
    fields = ("original_name", "kind", "mime_type", "size", "width", "height", "uploader", "created_at")
    readonly_fields = ("original_name", "kind", "mime_type", "size", "width", "height", "uploader", "created_at")
    can_delete = True

    def has_add_permission(self, request, obj=None):
        # Files are uploaded through the portal, not typed into the admin.
        return False


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "is_published", "created_at", "updated_at")
    list_filter = ("is_published",)
    search_fields = ("title", "body")
    inlines = [AttachmentInline]


@admin.register(DiscussionTopic)
class DiscussionTopicAdmin(admin.ModelAdmin):
    list_display = ("title", "created_by", "created_at", "updated_at")
    search_fields = ("title",)


@admin.register(DiscussionPost)
class DiscussionPostAdmin(admin.ModelAdmin):
    list_display = ("topic", "author", "created_at", "updated_at")
    search_fields = ("body", "author__username", "topic__title")
    inlines = [AttachmentInline]


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    """Read-and-delete view of attachments; blobs are only ever created by the portal.

    ``announcement``/``post``/``uploader`` are readonly so the admin cannot re-parent
    an existing blob onto content whose audience rules it never passed, and adding a
    row here is disabled because the readonly file field cannot produce a blob.
    """

    list_display = ("original_name", "kind", "size", "uploader", "announcement", "post", "created_at")
    list_filter = ("kind", "mime_type")
    search_fields = ("original_name", "uploader__username")
    fields = ("download", "original_name", "kind", "mime_type", "size", "width", "height",
              "uploader", "announcement", "post", "created_at")
    readonly_fields = fields

    def has_add_permission(self, request):
        # Files are uploaded through the portal; a row created here would have no blob.
        return False

    @admin.display(description="文件")
    def download(self, obj):
        if obj is None or not obj.pk or not obj.file:
            return "—"
        # The private root is not web-served, so link through the login-gated view.
        url = reverse("attachment-serve", args=[obj.pk])
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', url, obj.original_name)
