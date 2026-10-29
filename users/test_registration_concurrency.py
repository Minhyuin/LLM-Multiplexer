"""Exercise registration transactions against PostgreSQL, never an upstream CPA."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings

from api_keys.models import APIKey
from users.models import InvitationCode, RegistrationProvision
from users.registration import register_member, sync_registration_key


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RegistrationConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.invitation = InvitationCode.objects.create(code="CONCURRENCY-TEST", valid_days=7)

    def payload(self, name, username, invitation=None):
        return {
            "display_name": name, "username": username,
            "invitation_code": invitation or self.invitation.code,
            "lab_group": "算法",
            "password1": "Test-Only-Strong-Pass!93",
        }

    def concurrently(self, functions):
        start = Barrier(len(functions))

        def run(fn):
            connections.close_all()
            try:
                start.wait(timeout=10)
                return fn()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(run, fn) for fn in functions]
            return [future.result(timeout=25) for future in futures]

    def test_shared_invitation_allows_two_concurrent_members(self):
        ids = self.concurrently([
            lambda: register_member(self.payload("张三", "ZhangSan")).pk,
            lambda: register_member(self.payload("李四", "LiSi")).pk,
        ])
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(APIKey.objects.count(), 2)
        self.assertEqual(RegistrationProvision.objects.count(), 2)

    def test_same_username_across_two_invites_creates_only_one_account(self):
        second = InvitationCode.objects.create(code="OTHER-CONCURRENCY-TEST", valid_days=7)
        ready_to_insert = Barrier(2)
        create_user = User.objects.create_user

        def insert_together(*args, **kwargs):
            ready_to_insert.wait(timeout=10)
            return create_user(*args, **kwargs)

        def signup(code):
            try:
                register_member(self.payload("张三", "ZhangSan", code))
                return "created"
            except ValidationError:
                return "duplicate"

        with patch("users.registration.User.objects.create_user", side_effect=insert_together):
            results = self.concurrently([
                lambda: signup(self.invitation.code), lambda: signup(second.code),
            ])
        self.assertCountEqual(results, ["created", "duplicate"])
        self.assertEqual(User.objects.filter(username="ZhangSan").count(), 1)
        self.assertEqual(APIKey.objects.count(), 1)
        self.assertEqual(RegistrationProvision.objects.count(), 1)

    def test_simultaneous_syncs_push_and_activate_only_once(self):
        user = register_member(self.payload("张三", "ZhangSan"))
        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            results = self.concurrently([
                lambda: sync_registration_key(user.pk), lambda: sync_registration_key(user.pk),
            ])
        self.assertEqual(results, [True, True])
        push.assert_called_once()
        provision = RegistrationProvision.objects.get(user=user)
        self.assertEqual(provision.attempts, 1)
        self.assertIsNotNone(provision.synced_at)
        self.assertEqual(provision.api_key.status, APIKey.STATUS_ACTIVE)
