from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Announcement, DiscussionPost, DiscussionTopic


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
