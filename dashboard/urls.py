from django.urls import path

from dashboard import views

urlpatterns = [
    path("public/modules/<str:module>/", views.public_dashboard_module, name="public-dashboard-module"),
    path("admin/modules/<str:module>/", views.admin_dashboard_module, name="admin-dashboard-module"),
    path("public/", views.public_dashboard, name="public-dashboard"),
    path("admin/", views.admin_dashboard, name="admin-dashboard"),
    path("admin/pricing/", views.model_pricing_admin, name="admin-model-pricing"),
    path("admin/credentials/", views.admin_credentials, name="admin-credentials"),
    path("admin/credentials/health/", views.admin_credentials_health, name="admin-credentials-health"),
    path("admin/credentials/quota-cache/", views.admin_credentials_quota_cache, name="admin-credentials-quota-cache"),
    path("admin/credentials/refresh/<str:auth_index>/", views.admin_credentials_refresh, name="admin-credentials-refresh"),
]
