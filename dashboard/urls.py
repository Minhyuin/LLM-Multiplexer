from django.urls import path

from dashboard import views
from fingerprints import views as fingerprint_views

urlpatterns = [
    path("fingerprints/state/", fingerprint_views.state, name="fingerprint-state"),
    path("fingerprints/start/", fingerprint_views.start, name="fingerprint-start"),
    path("fingerprints/<uuid:run_id>/", fingerprint_views.detail, name="fingerprint-detail"),
    path("fingerprints/<uuid:run_id>/cancel/", fingerprint_views.cancel, name="fingerprint-cancel"),
    path("public/modules/<str:module>/", views.public_dashboard_module, name="public-dashboard-module"),
    path("admin/modules/<str:module>/", views.admin_dashboard_module, name="admin-dashboard-module"),
    path("public/model-usage/", views.public_model_usage, name="public-model-usage"),
    path("admin/model-usage/", views.admin_model_usage, name="admin-model-usage"),
    path("public/", views.public_dashboard, name="public-dashboard"),
    path("admin/", views.admin_dashboard, name="admin-dashboard"),
    path("admin/pricing/", views.model_pricing_admin, name="admin-model-pricing"),
    path("admin/queries/", views.query_trace_list, name="admin-query-traces"),
    path("admin/queries/<str:short_id>/", views.query_trace_detail, name="admin-query-trace-detail"),
    path("admin/credentials/", views.admin_credentials, name="admin-credentials"),
    path("admin/credentials/models/", views.admin_credentials_models_refresh, name="admin-credentials-models"),
    path("admin/credentials/health/", views.admin_credentials_health, name="admin-credentials-health"),
    path("admin/credentials/quota-cache/", views.admin_credentials_quota_cache, name="admin-credentials-quota-cache"),
    path("admin/credentials/refresh/<str:auth_index>/", views.admin_credentials_refresh, name="admin-credentials-refresh"),
]
