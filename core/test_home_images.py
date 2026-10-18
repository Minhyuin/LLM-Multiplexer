from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import Announcement, Attachment, DiscussionPost, DiscussionTopic


class HomeImageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="home-images")
        self.client.force_login(self.user)

    def image(self, **parent):
        return Attachment.objects.create(
            uploader=self.user, file="test.png", original_name="test.png",
            kind=Attachment.KIND_IMAGE, mime_type="image/png", width=1600,
            height=900, size=100, **parent,
        )

    def test_published_notice_images_render_but_drafts_and_documents_do_not(self):
        notice = Announcement.objects.create(title="Published", body="Body", is_published=True)
        image = self.image(announcement=notice)
        draft = Announcement.objects.create(title="Draft", body="Hidden")
        private_image = self.image(announcement=draft)
        document = Attachment.objects.create(announcement=notice, file="test.pdf", original_name="test.pdf", mime_type="application/pdf")
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'src="' + reverse("attachment-serve", args=[image.pk]) + '"')
        self.assertNotContains(response, reverse("attachment-serve", args=[private_image.pk]))
        self.assertNotContains(response, reverse("attachment-serve", args=[document.pk]))
        self.assertContains(response, 'width="1600" height="900" loading="lazy"')

    def test_opening_and_reply_images_are_assigned_to_their_own_topic(self):
        topics = [DiscussionTopic.objects.create(title=str(i), created_by=self.user) for i in range(2)]
        first = DiscussionPost.objects.create(topic=topics[0], author=self.user, body="Opening")
        reply = DiscussionPost.objects.create(topic=topics[0], author=self.user, body="Reply")
        other = DiscussionPost.objects.create(topic=topics[1], author=self.user, body="Other")
        expected = {topics[0].pk: [self.image(post=first).pk, self.image(post=reply).pk], topics[1].pk: [self.image(post=other).pk]}
        response = self.client.get(reverse("home"))
        for topic in response.context["discussion_topics"]:
            self.assertEqual([image.pk for image in topic.home_images], expected[topic.pk])
        for image_ids in expected.values():
            for image_id in image_ids:
                self.assertContains(response, 'src="' + reverse("attachment-serve", args=[image_id]) + '"', count=1)

    def test_image_lookup_count_does_not_grow_with_topic_count(self):
        topic = DiscussionTopic.objects.create(title="One", created_by=self.user)
        self.image(post=DiscussionPost.objects.create(topic=topic, author=self.user, body="Post"))
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("home"))
        initial = len(queries)
        for i in range(6):
            topic = DiscussionTopic.objects.create(title=str(i), created_by=self.user)
            self.image(post=DiscussionPost.objects.create(topic=topic, author=self.user, body="Post"))
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse("home"))
        self.assertEqual(len(queries), initial)

    def rendered_image_ids(self, response):
        html = response.content.decode()
        return [image.pk for image in Attachment.objects.order_by("pk") if f'src="{reverse("attachment-serve", args=[image.pk])}"' in html]

    def test_discussion_preview_stops_at_three_images_and_links_to_the_first_hidden_one(self):
        topic = DiscussionTopic.objects.create(title="Many", created_by=self.user)
        images = [self.image(post=DiscussionPost.objects.create(topic=topic, author=self.user, body=f"Post {i}")) for i in range(5)]
        response = self.client.get(reverse("home"))
        self.assertEqual(self.rendered_image_ids(response), [image.pk for image in images[:3]])
        self.assertEqual(response.context["discussion_topics"][0].home_image_total, 5)
        self.assertContains(response, "查看详细（共 5 张）")
        self.assertContains(response, f'{reverse("discussion-detail", args=[topic.pk])}#post-{images[3].post_id}')

    def test_three_images_need_no_overflow_link(self):
        topic = DiscussionTopic.objects.create(title="Three", created_by=self.user)
        images = [self.image(post=DiscussionPost.objects.create(topic=topic, author=self.user, body=f"Post {i}")) for i in range(3)]
        response = self.client.get(reverse("home"))
        self.assertEqual(self.rendered_image_ids(response), [image.pk for image in images])
        self.assertNotContains(response, "查看详细")
        # A full row equalises widths; fewer images keep their natural size.
        self.assertContains(response, "home-content-images--row")

    def test_two_images_keep_their_natural_size(self):
        topic = DiscussionTopic.objects.create(title="Two", created_by=self.user)
        images = [self.image(post=DiscussionPost.objects.create(topic=topic, author=self.user, body=f"Post {i}")) for i in range(2)]
        response = self.client.get(reverse("home"))
        self.assertEqual(self.rendered_image_ids(response), [image.pk for image in images])
        self.assertNotContains(response, "home-content-images--row")

    def test_notice_previews_share_the_three_image_cap(self):
        notice = Announcement.objects.create(title="Photos", body="Body", is_published=True)
        images = [self.image(announcement=notice) for _ in range(4)]
        response = self.client.get(reverse("home"))
        self.assertEqual(self.rendered_image_ids(response), [image.pk for image in images[:3]])
        self.assertContains(response, "查看详细（共 4 张）")
        self.assertContains(response, f'{reverse("announcement-list")}#notice-{notice.pk}')
