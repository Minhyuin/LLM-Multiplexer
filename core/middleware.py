from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.utils.cache import add_never_cache_headers


class PortalLoginRequiredMiddleware:
    """Protect application views; only login and the minimal probe are public.

    Static assets are served separately. Uploaded media must NOT be made public
    by a reverse proxy; an application middleware cannot protect proxy aliases.
    """

    public_view_names = {"login", "admin:login", "health-check"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.user.is_authenticated:
            add_never_cache_headers(response)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.user.is_authenticated:
            return None
        if request.resolver_match.view_name in self.public_view_names:
            return None
        return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
