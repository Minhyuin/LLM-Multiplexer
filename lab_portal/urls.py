from django.contrib import admin
from django.urls import include, path
from users.registration_views import register

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/register/", register, name="register"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
    path("users/", include("users.urls")),
    path("keys/", include("api_keys.urls")),
    path("usage/", include("usage.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("quota/", include("quota.urls")),
]
