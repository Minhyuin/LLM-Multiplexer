"""Cross-feature checks: borrowed identity must also apply to private attachments."""
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from core.models import Announcement, Attachment


class AccountAttachmentIntegrationTests(TestCase):
    def setUp(self):
        self.upload_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.upload_dir.cleanup)
        self.settings_override = override_settings(PRIVATE_UPLOAD_ROOT=Path(self.upload_dir.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        User = get_user_model()
        self.admin = User.objects.create_user('attachment-admin', is_staff=True)
        self.member = User.objects.create_user('attachment-member')
        self.other_admin = User.objects.create_user('attachment-other-admin', is_staff=True)
        self.draft = Announcement.objects.create(title='Draft', body='Private', is_published=False)
        self.published = Announcement.objects.create(title='Published', body='Team', is_published=True)
        self.draft_attachment = self.make_attachment(self.draft)
        self.published_attachment = self.make_attachment(self.published)
        self.client.force_login(self.admin)

    def make_attachment(self, announcement):
        return Attachment.objects.create(
            announcement=announcement, uploader=self.admin,
            file=ContentFile(b'attachment integration test', name='notes.txt'),
            original_name='notes.txt', mime_type='text/plain', size=27,
        )

    def serve_attachment(self, attachment):
        response = self.client.get(reverse('attachment-serve', args=[attachment.pk]))
        self.addCleanup(response.close)
        return response

    def enter(self, user):
        response = self.client.post(reverse('view-as-start', args=[user.pk]))
        self.assertEqual(response.status_code, 302)

    def test_member_view_uses_member_attachment_visibility_and_exit_restores_admin(self):
        self.assertEqual(self.serve_attachment(self.draft_attachment).status_code, 200)
        self.enter(self.member)
        response = self.serve_attachment(self.draft_attachment)
        self.assertIn(response.status_code, (403, 404))
        response = self.serve_attachment(self.published_attachment)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertEqual(self.client.post(reverse('view-as-stop')).status_code, 302)
        self.assertEqual(self.serve_attachment(self.draft_attachment).status_code, 200)

    def test_borrowed_admin_cannot_delete_attachment_even_with_target_permissions(self):
        self.enter(self.other_admin)
        response = self.client.post(reverse('attachment-delete', args=[self.published_attachment.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Attachment.objects.filter(pk=self.published_attachment.pk).exists())
        self.assertTrue(Path(self.published_attachment.file.path).exists())

    def test_borrowed_view_cannot_publish_or_upload_announcement(self):
        self.enter(self.other_admin)
        response = self.client.post(reverse('announcement-create'), {
            'title': 'Should not create', 'body': 'Readonly', 'is_published': 'on',
            'attachments': ContentFile(b'forbidden', name='notes.txt'),
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Announcement.objects.count(), 2)
        self.assertEqual(Attachment.objects.count(), 2)
