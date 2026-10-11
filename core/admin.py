from django.contrib import admin

from core.models import Announcement, DiscussionPost, DiscussionTopic


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "is_published", "created_at", "updated_at")
    list_filter = ("is_published",)
    search_fields = ("title", "body")


@admin.register(DiscussionTopic)
class DiscussionTopicAdmin(admin.ModelAdmin):
    list_display = ("title", "created_by", "created_at", "updated_at")
    search_fields = ("title",)


@admin.register(DiscussionPost)
class DiscussionPostAdmin(admin.ModelAdmin):
    list_display = ("topic", "author", "created_at", "updated_at")
    search_fields = ("body", "author__username", "topic__title")
