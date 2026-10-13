from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.version import STATIC_VERSION_SOURCES, portal_release_version, static_asset_version


class PortalReleaseVersionTests(SimpleTestCase):
    def test_running_release_marker_wins_and_tracks_rollback(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory), PORTAL_RELEASE_VERSION="v9.0.0"):
            marker = Path(directory) / ".release"
            marker.write_text("tag=v1.3.0\ncommit=example\n", encoding="utf-8")
            self.assertEqual(portal_release_version(), "v1.3.0")
            marker.write_text("tag=v1.2.0\n", encoding="utf-8")
            self.assertEqual(portal_release_version(), "v1.2.0")

    def test_missing_marker_supports_explicit_release_or_development(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory), PORTAL_RELEASE_VERSION="v1.3.1"):
            self.assertEqual(portal_release_version(), "v1.3.1")
            with override_settings(PORTAL_RELEASE_VERSION=""):
                self.assertEqual(portal_release_version(), "开发版本")

    def test_invalid_marker_and_override_do_not_show_arbitrary_metadata(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory), PORTAL_RELEASE_VERSION="<script>alert(1)</script>"):
            (Path(directory) / ".release").write_text("tag=not-a-release\ncommit=secret\n", encoding="utf-8")
            self.assertEqual(portal_release_version(), "开发版本")


class HomeReleaseVersionTests(TestCase):
    def test_home_shows_release_after_team_motto(self):
        self.client.force_login(User.objects.create_user(username="version-member"))
        with patch("core.views.portal_release_version", return_value="v1.3.0"):
            response = self.client.get(reverse("home"))
        self.assertContains(response, "门户版本 · v1.3.0")
        content = response.content.decode()
        self.assertLess(content.index("艰苦奋斗，求实创新，开疆破土，拓海立浪"), content.index("门户版本 · v1.3.0"))


class StaticAssetVersionTests(SimpleTestCase):
    """nginx serves /static/ immutable for 30 days, so the stylesheet URL must change per release."""

    def tearDown(self):
        static_asset_version.cache_clear()

    def test_token_stays_url_safe_without_a_release_marker(self):
        with TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory), STATICFILES_DIRS=(), PORTAL_RELEASE_VERSION=""):
            static_asset_version.cache_clear()
            token = static_asset_version()
        self.assertRegex(token, r"^[0-9A-Za-z.+-]+$")
        self.assertTrue(token.startswith("dev-"))

    def test_token_changes_when_a_stylesheet_changes(self):
        with TemporaryDirectory() as directory:
            static = Path(directory) / "static"
            (static / "css").mkdir(parents=True)
            for name in STATIC_VERSION_SOURCES:
                (static / name).write_text("body{color:red}", encoding="utf-8")
            with override_settings(BASE_DIR=Path(directory), STATICFILES_DIRS=(), PORTAL_RELEASE_VERSION="v1.3.1"):
                static_asset_version.cache_clear()
                before = static_asset_version()
                (static / "css" / "attachments.css").write_text("body{color:blue}", encoding="utf-8")
                static_asset_version.cache_clear()
                after = static_asset_version()
        self.assertTrue(before.startswith("v1.3.1-"))
        self.assertNotEqual(before, after)

    def test_token_differs_between_releases_with_identical_css(self):
        with TemporaryDirectory() as directory:
            static = Path(directory) / "static"
            (static / "css").mkdir(parents=True)
            for name in STATIC_VERSION_SOURCES:
                (static / name).write_text("body{color:red}", encoding="utf-8")
            with override_settings(BASE_DIR=Path(directory), STATICFILES_DIRS=(), PORTAL_RELEASE_VERSION="v1.3.1"):
                static_asset_version.cache_clear()
                first = static_asset_version()
            with override_settings(BASE_DIR=Path(directory), STATICFILES_DIRS=(), PORTAL_RELEASE_VERSION="v1.3.2"):
                static_asset_version.cache_clear()
                second = static_asset_version()
        self.assertNotEqual(first, second)


class VersionedStylesheetTests(TestCase):
    def tearDown(self):
        static_asset_version.cache_clear()

    def test_home_loads_every_stylesheet_through_the_version_token(self):
        self.client.force_login(User.objects.create_user(username="style-member"))
        token = static_asset_version()
        response = self.client.get(reverse("home"))
        content = response.content.decode()
        for name in ("site.css", "scandinavian.css", "attachments.css"):
            self.assertIn(f"/static/css/{name}?v={token}", content)
        self.assertNotIn("attachments.css?v=2", content)
        self.assertNotIn("site.css?v=20260513a", content)
