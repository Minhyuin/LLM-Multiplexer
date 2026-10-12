import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.attachments import save_attachments
from core.models import Announcement, Attachment, DiscussionPost, DiscussionTopic


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class CommunityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user("owner", password="test-password")
        cls.other = User.objects.create_user("other", password="test-password")
        cls.staff = User.objects.create_user("staff", is_staff=True)
        cls.role_admin = User.objects.create_user("role-admin")
        cls.role_admin.profile.role = "admin"
        cls.role_admin.profile.save()
        cls.topic = DiscussionTopic.objects.create(title="A discussion", created_by=cls.owner)
        cls.post = DiscussionPost.objects.create(topic=cls.topic, author=cls.owner, body="Original post")
        cls.notice = Announcement.objects.create(title="Published notice", body="Visible body", is_published=True)
        cls.draft = Announcement.objects.create(title="Secret draft", body="Hidden draft body")

    def login(self, user=None):
        self.client.force_login(user or self.owner)

    def test_anonymous_cannot_read_or_write_portal(self):
        paths = ["/", "/announcements/", "/announcements/admin/", "/discussions/",
                 f"/discussions/{self.topic.pk}/", "/discussions/new/", "/dashboard/public/",
                 "/usage/events/", "/quota/my/", "/users/admin/list/", "/accounts/password_change/"]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(urlsplit(response.url).path, "/accounts/login/")
                self.assertNotContains(response, "Original post", status_code=302)
        response = self.client.post(reverse("discussion-reply", args=[self.topic.pk]), {"body": "anonymous"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DiscussionPost.objects.count(), 1)

    def test_portal_branding_in_navigation_and_page_titles(self):
        import re
        from django.template.loader import render_to_string

        brand = "PIONEER AGENT ROUTER"
        login_response = self.client.get("/accounts/login/")
        self.assertContains(login_response, f"<title>登录 - {brand}</title>", html=True)
        self.assertNotContains(login_response, "PIONEER战队AI中转站")
        self.login()
        for path, title in [('/', '首页'), ('/announcements/', '公告栏'),
                            ('/discussions/', '讨论区'), ('/discussions/new/', '发起讨论')]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertContains(response, f'<title>{title} - {brand}</title>', html=True)
                self.assertContains(response, f'<span class="brand-title d-block">{brand}</span>', html=True)
                self.assertNotContains(response, "PIONEER战队AI中转站")
        self.login(self.staff)
        for path in ['/announcements/admin/', '/announcements/admin/create/',
                     reverse('discussion-detail', args=[self.topic.pk]),
                     reverse('discussion-edit', args=[self.post.pk]),
                     reverse('discussion-topic-edit', args=[self.topic.pk])]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            title = re.search(r'<title>(.*?)</title>', response.content.decode()).group(1)
            self.assertTrue(title.endswith(f' - {brand}'), title)
            self.assertEqual(title.count(brand), 1)
        base = render_to_string('base.html')
        self.assertIn(f'<title>首页 - {brand}</title>', base)

    def test_authenticated_pages_are_not_cacheable(self):
        self.login()
        for path in ['/', '/announcements/', '/discussions/']:
            response = self.client.get(path)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertIn('private', response['Cache-Control'])

    def test_public_exceptions_and_encoded_next(self):
        self.assertEqual(self.client.get("/accounts/login/").status_code, 200)
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})
        response = self.client.get("/discussions/?page=2&next=https://evil.invalid")
        self.assertEqual(parse_qs(urlsplit(response.url).query)["next"], ["/discussions/?page=2&next=https://evil.invalid"])

    def test_health_failure_does_not_leak_exception(self):
        with patch("django.db.connection.cursor", side_effect=RuntimeError("secret database details")):
            response = self.client.get("/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error"})

    def test_home_displays_published_only_and_removes_old_blocks(self):
        self.login()
        response = self.client.get("/")
        self.assertContains(response, "Published notice")
        self.assertNotContains(response, "Secret draft")
        for text in ["成员面板", "管理能力", "战队统计"]:
            self.assertNotContains(response, text)
        self.assertNotContains(response, "管理公告")

    def test_home_limits_and_list_pagination(self):
        Announcement.objects.bulk_create([Announcement(title=f"Notice {i}", body="x", is_published=True) for i in range(26)])
        DiscussionTopic.objects.bulk_create([DiscussionTopic(title=f"Topic {i}", created_by=self.owner) for i in range(26)])
        self.login()
        home = self.client.get("/")
        self.assertEqual(len(home.context["announcements"]), 5)
        self.assertEqual(len(home.context["discussion_topics"]), 8)
        for path in ["/announcements/", "/discussions/"]:
            response = self.client.get(path)
            self.assertEqual(len(response.context["page_obj"]), 20)
            self.assertTrue(response.context["page_obj"].has_next())
            self.assertEqual(self.client.get(path + "?page=invalid").status_code, 200)

    def test_member_cannot_manage_announcements(self):
        self.login()
        for url in [reverse("announcement-admin-list"), reverse("announcement-create"),
                    reverse("announcement-edit", args=[self.notice.pk]), reverse("announcement-delete", args=[self.notice.pk])]:
            self.assertEqual(self.client.post(url, {"title": "forged", "body": "forged"}).status_code, 403)
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.title, "Published notice")

    def test_both_admin_roles_can_manage_announcements(self):
        for admin in [self.staff, self.role_admin]:
            with self.subTest(user=admin.username):
                self.login(admin)
                self.assertContains(self.client.get("/"), "管理公告")
                self.assertEqual(self.client.get(reverse("announcement-create")).status_code, 200)
                response = self.client.post(reverse("announcement-create"), {"title": "New", "body": "Content", "is_published": "on"})
                self.assertEqual(response.status_code, 302)
                item = Announcement.objects.get(title="New")
                self.assertTrue(item.is_published)
                self.assertEqual(self.client.post(reverse("announcement-edit", args=[item.pk]), {"title": "Edited", "body": "Saved"}).status_code, 302)
                item.refresh_from_db()
                self.assertFalse(item.is_published)
                self.assertEqual(self.client.get(reverse("announcement-delete", args=[item.pk])).status_code, 405)
                self.assertEqual(self.client.post(reverse("announcement-delete", args=[item.pk])).status_code, 302)
                self.assertFalse(Announcement.objects.filter(pk=item.pk).exists())

    def test_create_and_reply_ignore_forged_owner(self):
        self.login()
        response = self.client.post(reverse("discussion-create"), {"title": "New topic", "body": "First", "created_by": self.other.pk, "author": self.other.pk})
        self.assertEqual(response.status_code, 302)
        topic = DiscussionTopic.objects.get(title="New topic")
        self.assertEqual(topic.created_by, self.owner)
        self.assertEqual(topic.posts.get().author, self.owner)
        old_updated = topic.updated_at
        self.login(self.other)
        response = self.client.post(reverse("discussion-reply", args=[topic.pk]), {"body": "Reply", "author": self.owner.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(topic.posts.get(body="Reply").author, self.other)
        topic.refresh_from_db()
        self.assertGreater(topic.updated_at, old_updated)

    def test_empty_or_oversized_posts_do_not_create_topics(self):
        self.login()
        for body in ["", "   ", "x" * 10001]:
            self.client.post(reverse("discussion-create"), {"title": "Invalid", "body": body})
        self.assertFalse(DiscussionTopic.objects.filter(title="Invalid").exists())
        response = self.client.post(reverse("discussion-reply", args=[self.topic.pk]), {"body": ""})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Original post", status_code=400)

    def test_edit_and_delete_other_members_posts_forbidden(self):
        self.login(self.other)
        for name in ["discussion-edit", "discussion-delete"]:
            self.assertEqual(self.client.post(reverse(name, args=[self.post.pk]), {"body": "hijack"}).status_code, 403)
        self.post.refresh_from_db()
        self.assertEqual(self.post.body, "Original post")

    def test_owner_can_edit_delete_without_deleting_others_replies(self):
        reply = DiscussionPost.objects.create(topic=self.topic, author=self.other, body="Keep me")
        self.login()
        self.assertEqual(self.client.get(reverse("discussion-edit", args=[self.post.pk])).status_code, 200)
        self.assertEqual(self.client.post(reverse("discussion-edit", args=[self.post.pk]), {"body": "Edited"}).status_code, 302)
        self.post.refresh_from_db()
        self.assertEqual(self.post.body, "Edited")
        self.assertEqual(self.client.get(reverse("discussion-delete", args=[self.post.pk])).status_code, 405)
        self.assertEqual(self.client.post(reverse("discussion-delete", args=[self.post.pk])).status_code, 302)
        self.assertTrue(DiscussionPost.objects.filter(pk=reply.pk).exists())
        self.assertTrue(DiscussionTopic.objects.filter(pk=self.topic.pk).exists())

    def test_deleting_last_post_cleans_empty_topic(self):
        self.login()
        self.client.post(reverse("discussion-delete", args=[self.post.pk]))
        self.assertFalse(DiscussionTopic.objects.filter(pk=self.topic.pk).exists())

    def test_admins_can_edit_and_delete_any_post(self):
        for admin in [self.staff, self.role_admin]:
            post = DiscussionPost.objects.create(topic=self.topic, author=self.other, body="Moderate me")
            self.login(admin)
            self.assertEqual(self.client.post(reverse("discussion-edit", args=[post.pk]), {"body": "Moderated"}).status_code, 302)
            self.assertEqual(self.client.post(reverse("discussion-delete", args=[post.pk])).status_code, 302)
            self.assertFalse(DiscussionPost.objects.filter(pk=post.pk).exists())

    def test_topic_permissions(self):
        self.login(self.other)
        self.assertEqual(self.client.post(reverse("discussion-topic-edit", args=[self.topic.pk]), {"title": "bad"}).status_code, 403)
        self.login()
        self.assertEqual(self.client.post(reverse("discussion-topic-edit", args=[self.topic.pk]), {"title": "Renamed"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("discussion-topic-delete", args=[self.topic.pk])).status_code, 403)
        self.login(self.role_admin)
        self.assertEqual(self.client.post(reverse("discussion-topic-delete", args=[self.topic.pk])).status_code, 302)

    def test_plain_text_is_escaped(self):
        self.post.body = '<script>alert("xss")</script>'
        self.post.save()
        self.notice.body = self.post.body
        self.notice.save()
        self.login()
        for url in [reverse("discussion-detail", args=[self.topic.pk]), "/announcements/"]:
            response = self.client.get(url)
            self.assertContains(response, "&lt;script&gt;")
            self.assertNotContains(response, '<script>alert("xss")</script>')

    def test_post_pagination(self):
        DiscussionPost.objects.bulk_create([DiscussionPost(topic=self.topic, author=self.owner, body="More") for _ in range(24)])
        self.login()
        response = self.client.get(reverse("discussion-detail", args=[self.topic.pk]))
        self.assertEqual(len(response.context["page_obj"]), 20)
        self.assertTrue(response.context["page_obj"].has_next())

    def test_csrf_required_for_all_write_routes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        routes = [("discussion-create", []), ("discussion-reply", [self.topic.pk]),
                  ("discussion-edit", [self.post.pk]), ("discussion-delete", [self.post.pk]),
                  ("discussion-topic-edit", [self.topic.pk]), ("discussion-topic-delete", [self.topic.pk]),
                  ("announcement-create", []), ("announcement-edit", [self.notice.pk]),
                  ("announcement-delete", [self.notice.pk])]
        for name, args in routes:
            with self.subTest(name=name):
                self.assertEqual(client.post(reverse(name, args=args), {"body": "test", "title": "test"}).status_code, 403)
        client.get(reverse("discussion-create"))
        token = client.cookies["csrftoken"].value
        response = client.post(reverse("discussion-create"), {"title": "With token", "body": "ok", "csrfmiddlewaretoken": token})
        self.assertEqual(response.status_code, 302)


class AttachmentTests(TestCase):
    """Private attachment upload, serving, permission and cleanup behaviour.

    Every test runs against a temporary PRIVATE_UPLOAD_ROOT, so nothing is written
    to the real private root or to MEDIA_ROOT.
    """

    def setUp(self):
        # A fresh private root per test: TestCase rolls the rows back but not the
        # files on disk, so isolation has to come from the directory itself.
        self.private_root = tempfile.mkdtemp(prefix="portal-attach-")
        self._override = override_settings(PRIVATE_UPLOAD_ROOT=self.private_root)
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self.private_root, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create_user("attach-owner", password="test-password")
        cls.other = User.objects.create_user("attach-other", password="test-password")
        cls.staff = User.objects.create_user("attach-staff", is_staff=True)
        cls.topic = DiscussionTopic.objects.create(title="Attachments", created_by=cls.owner)
        cls.post = DiscussionPost.objects.create(topic=cls.topic, author=cls.owner, body="With files")
        cls.notice = Announcement.objects.create(title="Notice", body="Body", is_published=True)
        cls.draft = Announcement.objects.create(title="Draft", body="Draft body")

    def login(self, user=None):
        self.client.force_login(user or self.owner)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def image_upload(name="pic.png", size=(6, 4), fmt="PNG", color=(180, 40, 40)):
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", size, color).save(buffer, format=fmt)
        content_type = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}[fmt]
        return SimpleUploadedFile(name, buffer.getvalue(), content_type=content_type)

    @staticmethod
    def text_upload(name="notes.txt", body=b"hello world"):
        return SimpleUploadedFile(name, body, content_type="text/plain")

    def private_files(self):
        root = Path(settings.PRIVATE_UPLOAD_ROOT)
        if not root.exists():
            return []
        return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())

    def make_attachment(self, parent, uploader=None, upload=None, **kwargs):
        upload = upload or self.image_upload()
        return save_attachments([upload], uploader or self.owner, **{parent: kwargs.pop("parent_obj")})[0]

    def attach_to_post(self, post=None, uploader=None, upload=None):
        post = post or self.post
        return save_attachments([upload or self.image_upload()], uploader or self.owner, post=post)[0]

    def attach_to_announcement(self, announcement=None, uploader=None, upload=None):
        announcement = announcement or self.notice
        return save_attachments([upload or self.image_upload()], uploader or self.owner, announcement=announcement)[0]

    # --- form wiring ---------------------------------------------------------

    def test_attachment_field_is_declared_and_renders_multiple_input(self):
        from core.forms import AnnouncementForm, DiscussionPostForm

        for form_class in (AnnouncementForm, DiscussionPostForm):
            with self.subTest(form=form_class.__name__):
                form = form_class()
                self.assertIn("attachments", form.fields)
                field = form.fields["attachments"]
                self.assertTrue(field.widget.allow_multiple_selected)
                html = str(form["attachments"])
                self.assertIn('type="file"', html)
                self.assertIn("multiple", html)
                self.assertIn("attachments", html)

    def test_announcement_form_multipart_and_upload_count(self):
        self.login(self.staff)
        response = self.client.get(reverse("announcement-create"))
        self.assertContains(response, 'enctype="multipart/form-data"')
        self.assertContains(response, "multiple")

    # --- storage location ----------------------------------------------------

    def test_private_root_default_is_outside_media_root(self):
        from django.conf import settings as live_settings

        private_root = Path(str(live_settings.PRIVATE_UPLOAD_ROOT)).resolve()
        media_root = Path(str(live_settings.MEDIA_ROOT)).resolve()
        self.assertNotEqual(private_root, media_root)
        self.assertNotIn(str(media_root), str(private_root))
        self.assertTrue(str(live_settings.PRIVATE_UPLOAD_URL).startswith("/"))

    def test_uploads_land_in_private_root_with_uuid_names(self):
        self.login(self.staff)
        response = self.client.post(
            reverse("announcement-create"),
            {"title": "Files", "body": "See attached", "is_published": "on",
             "attachments": [self.image_upload("diagram.png"), self.text_upload("notes.txt")]},
        )
        self.assertEqual(response.status_code, 302)
        announcement = Announcement.objects.get(title="Files")
        attachments = list(announcement.attachments.order_by("pk"))
        self.assertEqual(len(attachments), 2)
        for attachment in attachments:
            stored = Path(settings.PRIVATE_UPLOAD_ROOT) / attachment.file.name
            self.assertTrue(stored.exists(), attachment.file.name)
            self.assertNotIn(attachment.original_name, attachment.file.name)
            self.assertNotIn(str(Path(settings.MEDIA_ROOT)), str(stored))
        image, document = attachments
        self.assertEqual(image.kind, "image")
        self.assertEqual(image.mime_type, "image/png")
        self.assertEqual((image.width, image.height), (6, 4))
        self.assertEqual(image.original_name, "diagram.png")
        self.assertEqual(document.kind, "document")
        self.assertGreater(document.size, 0)
        self.assertEqual(len(self.private_files()), 2)

    # --- serving -------------------------------------------------------------

    def test_image_served_inline_and_document_forced_download(self):
        image = self.attach_to_announcement()
        document = self.attach_to_announcement(upload=self.text_upload("report.txt"))
        self.login()
        response = self.client.get(reverse("attachment-serve", args=[image.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        download = self.client.get(reverse("attachment-serve", args=[document.pk]))
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertIn("report.txt", download["Content-Disposition"])
        self.assertEqual(download["X-Content-Type-Options"], "nosniff")

    def test_anonymous_cannot_serve_and_missing_is_404(self):
        image = self.attach_to_announcement()
        response = self.client.get(reverse("attachment-serve", args=[image.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response.url).path, "/accounts/login/")
        self.login()
        self.assertEqual(self.client.get(reverse("attachment-serve", args=[999999])).status_code, 404)

    def test_draft_announcement_attachments_hidden_from_members(self):
        draft_attachment = self.attach_to_announcement(announcement=self.draft)
        self.login(self.other)
        self.assertEqual(self.client.get(reverse("attachment-serve", args=[draft_attachment.pk])).status_code, 404)
        self.assertNotContains(self.client.get("/announcements/"), draft_attachment.original_name)
        self.login(self.staff)
        self.assertEqual(self.client.get(reverse("attachment-serve", args=[draft_attachment.pk])).status_code, 200)

    def test_discussion_attachment_served_to_signed_in_members(self):
        attachment = self.attach_to_post()
        self.login(self.other)
        self.assertEqual(self.client.get(reverse("attachment-serve", args=[attachment.pk])).status_code, 200)
        detail = self.client.get(reverse("discussion-detail", args=[self.topic.pk]))
        self.assertContains(detail, reverse("attachment-serve", args=[attachment.pk]))

    # --- removal permissions / CSRF ------------------------------------------

    def test_only_uploader_or_admin_can_delete_attachment(self):
        attachment = self.attach_to_post(uploader=self.owner)
        self.login(self.other)
        self.assertEqual(self.client.post(reverse("attachment-delete", args=[attachment.pk])).status_code, 403)
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())
        self.login(self.owner)
        self.assertEqual(self.client.get(reverse("attachment-delete", args=[attachment.pk])).status_code, 405)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("attachment-delete", args=[attachment.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertEqual(self.private_files(), [])

    def test_admin_can_delete_someone_elses_attachment(self):
        attachment = self.attach_to_post(uploader=self.owner)
        self.login(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(reverse("attachment-delete", args=[attachment.pk])).status_code, 302)
        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_attachment_delete_requires_csrf(self):
        attachment = self.attach_to_post(uploader=self.owner)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(reverse("attachment-delete", args=[attachment.pk])).status_code, 403)
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    # --- validation ----------------------------------------------------------

    def test_invalid_image_rejected_without_writing_anything(self):
        self.login(self.staff)
        broken = SimpleUploadedFile("broken.png", b"\x89PNG\r\n\x1a\nnot-an-image", content_type="image/png")
        response = self.client.post(
            reverse("announcement-create"),
            {"title": "Bad", "body": "x", "attachments": [broken]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "图片无法解码")
        self.assertFalse(Announcement.objects.filter(title="Bad").exists())
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertEqual(self.private_files(), [])

    def test_extension_content_mismatch_rejected(self):
        self.login(self.owner)
        fake = SimpleUploadedFile("photo.png", b"plain text pretending to be a png", content_type="image/png")
        response = self.client.post(reverse("discussion-reply", args=[self.topic.pk]), {"body": "hi", "attachments": [fake]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertEqual(self.private_files(), [])

    def test_svg_and_executables_are_rejected(self):
        self.login(self.staff)
        for name, payload in [("vector.svg", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
                              ("run.exe", b"MZ\x90\x00binary"),
                              ("evil.php.jpg", b"\xff\xd8\xff\xe0<?php ?>")]:
            with self.subTest(name=name):
                response = self.client.post(
                    reverse("announcement-create"),
                    {"title": "Nope", "body": "x", "attachments": [SimpleUploadedFile(name, payload)]},
                )
                self.assertEqual(response.status_code, 200)
                self.assertFalse(Announcement.objects.filter(title="Nope").exists())
        self.assertEqual(self.private_files(), [])

    @override_settings(ATTACHMENT_MAX_BYTES=2048)
    def test_oversized_single_file_rejected(self):
        self.login(self.owner)
        big = SimpleUploadedFile("big.txt", b"a" * 4096, content_type="text/plain")
        response = self.client.post(reverse("discussion-reply", args=[self.topic.pk]), {"body": "hi", "attachments": [big]})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "单个文件不能超过", status_code=400)
        self.assertEqual(self.private_files(), [])

    @override_settings(ATTACHMENT_MAX_BATCH_BYTES=1024)
    def test_aggregate_batch_limit_rejected(self):
        self.login(self.owner)
        files = [SimpleUploadedFile(f"part{i}.txt", b"x" * 800) for i in range(3)]
        response = self.client.post(reverse("discussion-reply", args=[self.topic.pk]), {"body": "hi", "attachments": files})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "总大小", status_code=400)
        self.assertEqual(self.private_files(), [])

    @override_settings(ATTACHMENT_MAX_PER_CONTENT=1)
    def test_per_content_count_cap_includes_existing_attachments(self):
        self.attach_to_post()
        self.login(self.owner)
        response = self.client.post(reverse("discussion-edit", args=[self.post.pk]),
                                    {"body": "edited", "attachments": [self.text_upload("second.txt")]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "最多 1 个附件")
        self.assertEqual(Attachment.objects.count(), 1)

    @override_settings(ATTACHMENT_MAX_PIXELS=10)
    def test_pixel_bomb_rejected(self):
        self.login(self.owner)
        response = self.client.post(reverse("discussion-reply", args=[self.topic.pk]),
                                    {"body": "hi", "attachments": [self.image_upload(size=(40, 40))]})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "图片尺寸过大", status_code=400)
        self.assertEqual(self.private_files(), [])

    # --- cleanup / transactional lifecycle -----------------------------------

    def test_second_blob_write_failure_removes_first_blob(self):
        from core.attachments import PrivateAttachmentStorage

        real_save = PrivateAttachmentStorage.save
        calls = {"n": 0}

        def flaky(self, name, content, max_length=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real_save(self, name, content, max_length)

        with patch.object(PrivateAttachmentStorage, "save", flaky):
            with self.assertRaises(OSError):
                save_attachments([self.image_upload("a.png"), self.image_upload("b.png")], self.owner, post=self.post)
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertEqual(self.private_files(), [])

    def test_second_row_db_failure_removes_first_blob(self):
        real_save = Attachment.save
        calls = {"n": 0}

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("db down")
            return real_save(self, *args, **kwargs)

        with patch.object(Attachment, "save", flaky):
            with self.assertRaises(RuntimeError):
                save_attachments([self.image_upload("a.png"), self.image_upload("b.png")], self.owner, post=self.post)
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertEqual(self.private_files(), [])

    def test_real_integrity_error_keeps_caller_transaction_usable_and_cleans_blobs(self):
        """A genuine DB error must roll the batch back, not poison the caller.

        ``Model.save_base`` marks the connection for rollback as soon as an insert
        fails inside an atomic block, so a handler that deletes the already-saved
        rows would raise TransactionManagementError and leak the blobs instead.
        """
        real_save = Attachment.save
        calls = {"n": 0}

        def flaky(instance, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                # Violates the ``attachment_exactly_one_parent`` CHECK constraint,
                # so the database itself raises IntegrityError.
                instance.post = self.post
            return real_save(instance, *args, **kwargs)

        with patch.object(Attachment, "save", flaky):
            with transaction.atomic():
                with self.assertRaises(IntegrityError):
                    save_attachments([self.image_upload("one.png"), self.image_upload("two.png")],
                                     self.owner, announcement=self.draft)
                # The savepoint must leave the caller's transaction usable.
                self.assertEqual(Attachment.objects.count(), 0)
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertEqual(self.private_files(), [])

    def test_rollback_keeps_prior_blob_downloadable(self):
        attachment = self.attach_to_post()
        blob = Path(settings.PRIVATE_UPLOAD_ROOT) / attachment.file.name
        self.assertTrue(blob.exists())
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                self.post.delete()
                raise RuntimeError("boom")
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(blob.exists())

    def test_deleting_parent_removes_blobs_on_commit(self):
        attachment = self.attach_to_post()
        blob = Path(settings.PRIVATE_UPLOAD_ROOT) / attachment.file.name
        self.login(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(reverse("discussion-topic-delete", args=[self.topic.pk])).status_code, 302)
        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertFalse(blob.exists())

    def test_announcement_delete_removes_blob_on_commit(self):
        attachment = self.attach_to_announcement()
        blob = Path(settings.PRIVATE_UPLOAD_ROOT) / attachment.file.name
        self.login(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(reverse("announcement-delete", args=[self.notice.pk])).status_code, 302)
        self.assertFalse(blob.exists())

    def test_post_delete_keeps_other_members_replies_and_their_files(self):
        reply = DiscussionPost.objects.create(topic=self.topic, author=self.other, body="Keep me")
        keep = self.attach_to_post(post=reply, uploader=self.other)
        own = self.attach_to_post()
        own_blob = Path(settings.PRIVATE_UPLOAD_ROOT) / own.file.name
        keep_blob = Path(settings.PRIVATE_UPLOAD_ROOT) / keep.file.name
        self.login(self.owner)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(reverse("discussion-delete", args=[self.post.pk])).status_code, 302)
        self.assertTrue(Attachment.objects.filter(pk=keep.pk).exists())
        self.assertTrue(keep_blob.exists())
        self.assertFalse(Attachment.objects.filter(pk=own.pk).exists())
        self.assertFalse(own_blob.exists())

    # --- uploader lifecycle --------------------------------------------------

    def test_deleting_uploader_preserves_attachment_on_published_announcement(self):
        uploader = get_user_model().objects.create_user("temp-uploader")
        attachment = self.attach_to_announcement(announcement=self.notice, uploader=uploader)
        blob = Path(settings.PRIVATE_UPLOAD_ROOT) / attachment.file.name
        uploader.delete()
        attachment.refresh_from_db()
        self.assertIsNone(attachment.uploader_id)
        self.assertTrue(blob.exists())
        self.login(self.other)
        self.assertEqual(self.client.get(reverse("attachment-serve", args=[attachment.pk])).status_code, 200)

    def test_deleting_uploader_keeps_attachment_on_others_post(self):
        uploader = get_user_model().objects.create_user("temp-uploader-2")
        attachment = self.attach_to_post(uploader=uploader)
        blob = Path(settings.PRIVATE_UPLOAD_ROOT) / attachment.file.name
        uploader.delete()
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(blob.exists())
        # With the uploader gone only an admin may still remove it.
        self.login(self.owner)
        self.assertEqual(self.client.post(reverse("attachment-delete", args=[attachment.pk])).status_code, 403)
        self.login(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(reverse("attachment-delete", args=[attachment.pk])).status_code, 302)
        self.assertFalse(blob.exists())

    # --- editing -------------------------------------------------------------

    def test_editing_post_keeps_existing_attachments(self):
        attachment = self.attach_to_post()
        self.login(self.owner)
        response = self.client.post(reverse("discussion-edit", args=[self.post.pk]), {"body": "Edited body"})
        self.assertEqual(response.status_code, 302)
        self.post.refresh_from_db()
        self.assertEqual(self.post.body, "Edited body")
        self.assertTrue(Attachment.objects.filter(pk=attachment.pk).exists())

    def test_member_cannot_upload_attachments_to_announcement(self):
        self.login(self.owner)
        response = self.client.post(
            reverse("announcement-create"),
            {"title": "Sneaky", "body": "x", "attachments": [self.image_upload()]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertEqual(self.private_files(), [])

    # --- django admin --------------------------------------------------------

    def test_admin_is_readonly_and_links_to_protected_download(self):
        from django.contrib.admin.sites import AdminSite

        from core.admin import AttachmentAdmin

        attachment = self.attach_to_post()
        model_admin = AttachmentAdmin(Attachment, AdminSite())
        # No blob can be produced from the admin, so rows must not be creatable.
        self.assertFalse(model_admin.has_add_permission(None))
        # Re-parenting would move a blob to content it never passed the rules for.
        for name in ("announcement", "post", "uploader"):
            self.assertIn(name, model_admin.readonly_fields)
        self.assertNotIn("file", model_admin.readonly_fields)
        html = model_admin.download(attachment)
        self.assertIn(reverse("attachment-serve", args=[attachment.pk]), html)
        self.assertIn(attachment.original_name, html)
        self.assertEqual(model_admin.download(Attachment(pk=0)), "—")

    def test_admin_change_form_renders_protected_download_link(self):
        """The admin change page must stay renderable with the readonly field layout."""
        User = get_user_model()
        staff = User.objects.create_user("attach-super", password="test-password")
        staff.is_staff = True
        staff.is_superuser = True
        staff.save(update_fields=["is_staff", "is_superuser"])
        attachment = self.attach_to_post()
        self.client.force_login(staff)
        response = self.client.get(reverse("admin:core_attachment_change", args=[attachment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("attachment-serve", args=[attachment.pk]))
