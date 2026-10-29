from django.urls import path

from users import views, registration_views

urlpatterns = [
    path("profile/", views.profile_detail, name="profile"),
    path("admin/invitations/", registration_views.invitation_list, name="admin-invitation-list"),
    path("admin/invitations/<int:invitation_id>/revoke/", registration_views.invitation_revoke, name="admin-invitation-revoke"),
    path("admin/list/", views.admin_user_list, name="admin-user-list"),
    path("admin/create/", views.admin_user_create, name="admin-user-create"),
    path("admin/<int:user_id>/edit/", views.admin_user_edit, name="admin-user-edit"),
    path("admin/<int:user_id>/toggle/", views.admin_user_toggle, name="admin-user-toggle"),
    path("admin/<int:user_id>/delete/", views.admin_user_delete, name="admin-user-delete"),
    path("admin/<int:user_id>/view-as/", views.admin_user_view_as_start, name="view-as-start"),
    path("admin/view-as/stop/", views.admin_user_view_as_stop, name="view-as-stop"),
    path("admin/batch/", views.admin_user_batch, name="admin-user-batch"),
]
