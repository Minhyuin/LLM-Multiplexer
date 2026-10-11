from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from quota.activity import personal_activity
from usage.models import UsageLog


class PersonalActivityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user('activity-member')
        self.other = get_user_model().objects.create_user('activity-other')

    def log(self, when, user=None, error=False):
        UsageLog.objects.create(request_id=str(UsageLog.objects.count()), user=user or self.user,
                                request_time=when, model_name='model', is_error=error)

    def test_local_dates_bounds_user_isolation_and_error_calls(self):
        today = date(2026, 10, 11)
        tz = ZoneInfo('Asia/Shanghai')
        start = datetime(2025, 10, 12, tzinfo=tz)
        self.log(start)
        self.log(start - timedelta(microseconds=1))
        self.log(datetime(2026, 10, 11, 0, 1, tzinfo=tz), error=True)
        self.log(datetime(2026, 10, 11, 23, 59, tzinfo=tz))
        self.log(datetime(2026, 10, 12, tzinfo=tz))
        self.log(start, user=self.other)
        with timezone.override(tz), self.assertNumQueries(1):
            activity = personal_activity(self.user, today)
        days = [d for w in activity['weeks'] for d in w['days'] if d['valid']]
        self.assertEqual(len(days), 365)
        self.assertEqual(activity['total'], 3)
        self.assertEqual(activity['active_days'], 2)
        self.assertEqual(days[0]['date'], start.date())
        self.assertEqual(days[-1]['calls'], 2)

    def test_empty_and_level_thresholds(self):
        today = timezone.localdate()
        self.assertEqual(personal_activity(self.user)['total'], 0)
        for offset, count in enumerate([1, 9, 10, 49, 50, 99, 100]):
            when = timezone.now() - timedelta(days=offset)
            UsageLog.objects.bulk_create([UsageLog(request_id=f'{offset}-{i}', user=self.user,
                request_time=when, model_name='model') for i in range(count)])
        activity = personal_activity(self.user, today)
        levels = {d['date']: d['level'] for w in activity['weeks'] for d in w['days'] if d['valid']}
        self.assertEqual([levels[today-timedelta(days=i)] for i in range(7)], [1,1,2,2,3,3,4])

    def test_quota_requires_login_and_activity_ignores_range_filter(self):
        self.assertEqual(self.client.get('/quota/my/').status_code, 302)
        self.log(timezone.now()-timedelta(days=3))
        self.client.force_login(self.user)
        response = self.client.get('/quota/my/?range=today')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['activity']['total'], 1)
        self.assertContains(response, '我的调用活跃度')
        self.assertContains(response, 'activity-level-1')
