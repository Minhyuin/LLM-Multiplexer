from datetime import datetime, time, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from usage.models import UsageLog


def personal_activity(user, today=None):
    """Last 365 local dates, including today; one recorded request is one call."""
    today = today or timezone.localdate()
    start = today - timedelta(days=364)
    tz = timezone.get_current_timezone()
    lower = timezone.make_aware(datetime.combine(start, time.min), tz)
    upper = timezone.make_aware(datetime.combine(today + timedelta(days=1), time.min), tz)
    counts = dict(UsageLog.objects.filter(user=user, request_time__gte=lower, request_time__lt=upper)
                  .annotate(day=TruncDate('request_time', tzinfo=tz)).values('day')
                  .annotate(calls=Count('pk')).values_list('day', 'calls'))
    first = start - timedelta(days=(start.weekday() + 1) % 7)
    last = today + timedelta(days=6 - (today.weekday() + 1) % 7)
    weeks = []
    cursor = first
    while cursor <= last:
        week = {'month': '', 'days': []}
        for _ in range(7):
            valid = start <= cursor <= today
            calls = counts.get(cursor, 0) if valid else 0
            level = 0 if not calls else 1 if calls < 10 else 2 if calls < 50 else 3 if calls < 100 else 4
            if valid and (cursor.day == 1 or cursor == start):
                week['month'] = f'{cursor.month}月'
            week['days'].append({'date': cursor, 'calls': calls, 'level': level, 'valid': valid})
            cursor += timedelta(days=1)
        weeks.append(week)
    return {'weeks': weeks, 'total': sum(counts.values()), 'active_days': len(counts),
            'start': start, 'end': today}
