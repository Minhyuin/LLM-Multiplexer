from django.urls import path

from core import views

urlpatterns = [
    path("", views.home, name="home"),
    path("health/", views.health_check, name="health-check"),
    path("announcements/", views.announcement_public_list, name="announcement-list"),
    path("discussions/<int:topic_id>/edit/", views.discussion_topic_edit, name="discussion-topic-edit"),
    path("discussions/<int:topic_id>/delete/", views.discussion_topic_delete, name="discussion-topic-delete"),
    path("announcements/admin/", views.announcement_list, name="announcement-admin-list"),
    path("announcements/admin/create/", views.announcement_create, name="announcement-create"),
    path("announcements/admin/<int:announcement_id>/edit/", views.announcement_edit, name="announcement-edit"),
    path("announcements/admin/<int:announcement_id>/delete/", views.announcement_delete, name="announcement-delete"),
    path("discussions/", views.discussion_list, name="discussion-list"),
    path("discussions/new/", views.discussion_create, name="discussion-create"),
    path("discussions/<int:topic_id>/", views.discussion_detail, name="discussion-detail"),
    path("discussions/<int:topic_id>/reply/", views.discussion_reply, name="discussion-reply"),
    path("discussions/posts/<int:post_id>/edit/", views.discussion_edit, name="discussion-edit"),
    path("discussions/posts/<int:post_id>/delete/", views.discussion_delete, name="discussion-delete"),
]
