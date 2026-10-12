from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.version import portal_release_version


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
