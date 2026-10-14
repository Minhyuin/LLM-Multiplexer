"""Tests for the Key filter panels and key masking.

Two behaviours are covered here:

* the ``请求 Query 追溯`` Key filter, whose option list is the union of every
  historical ``api_key_identifier`` retained by :class:`QueryTrace` *and*
  :class:`UsageLog` -- independent of the time/member/model filters, deduplicated,
  still listing deleted keys, and reachable from UsageLog alone;
* the masked display rule (``*`` + bounded suffix: at most 5 characters on the
  trace pages, at most 10 on the request-events page), which must never leak a
  full identifier into the HTML or into a filter URL.

``usage.views.auto_sync_usage_queue`` is patched in every request-events test so
the page render never performs a real sync.
"""
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from core.key_identifiers import key_index, key_suffix
from usage.models import QueryTrace, UsageLog

User = get_user_model()

#: Two identifiers that share their last four characters, i.e. the case the
#: filter must never confuse even though the label looks identical.
IDENT_ALPHA = "labsk-collide-zzz1"
IDENT_BETA = "labsk-other01-zzz1"
IDENT_HISTORIC = "labsk-historical01"
IDENT_BOTH = "labsk-both000001"
IDENT_LONG = "labsk-longident01"
IDENT_SHORT = "labsk7"
IDENT_LEGACY = "labsk-deleted0001"

IDENTIFIERS = (
    IDENT_ALPHA,
    IDENT_BETA,
    IDENT_HISTORIC,
    IDENT_BOTH,
    IDENT_LONG,
    IDENT_SHORT,
    IDENT_LEGACY,
)

#: The 5-character trace-page label of IDENT_LONG ("labsk-longident01"[-4:]).
LABEL_LONG = "*nt01"
#: The 11-character request-events label of IDENT_LONG ("labsk-longident01"[-10:]).
EVENT_LABEL_LONG = "*ongident01"

TRACE_KEY_CELL = re.compile(r'<td class="text-nowrap font-monospace">(.*?)</td>', re.S)
EVENT_KEY_CELL = re.compile(r'<td class="ev-cell-key">(.*?)</td>', re.S)


class KeyPanelTestMixin:
    def setUp(self):
        self.admin = User.objects.create_user(username="key-panel-admin", is_staff=True)
        self.member = User.objects.create_user(username="key-panel-member")

    def trace(self, short_id, identifier, *, when=None, user=None, model="gpt-6.1-sol",
              query_text="排查用提问内容", **kwargs):
        return QueryTrace.objects.create(
            short_id=short_id,
            api_key_identifier=identifier,
            user=user,
            user_identifier=(user.username if user else ""),
            model_name=model,
            endpoint="/v1/chat/completions",
            query_text=query_text,
            query_chars=len(query_text),
            status_code=200,
            request_time=when or timezone.now(),
            **kwargs,
        )

    def usage_log(self, request_id, identifier, *, when=None, user=None, model="gpt-6.1-sol"):
        return UsageLog.objects.create(
            request_id=request_id,
            api_key_identifier=identifier,
            user=user,
            user_identifier=(user.username if user else ""),
            model_name=model,
            status_code=200,
            request_time=when or timezone.now(),
        )

    def trace_list(self, **params):
        response = self.client.get(reverse("admin-query-traces"), params)
        self.assertEqual(response.status_code, 200)
        return response

    def option_values(self, response):
        return [option["value"] for option in response.context["key_options"]]

    def option_labels(self, response):
        return [option["label"] for option in response.context["key_options"]]

    def option_for(self, response, identifier):
        """The rendered option entry for one identifier, looked up by its opaque index."""
        token = key_index(identifier)
        for option in response.context["key_options"]:
            if option["value"] == token:
                return option
        self.fail(f"{identifier!r} is missing from the Key filter options")

    def trace_key_cells(self, response):
        """Rendered text of the Key cell of every trace row, in row order."""
        cells = [cell.strip() for cell in TRACE_KEY_CELL.findall(response.content.decode())]
        self.assertEqual(len(cells), len(response.context["traces"]), "one Key cell per trace row")
        return cells

    def event_key_cells(self, response):
        """Rendered text of the 令牌 cell of every request-event row, in row order."""
        rows = list(response.context["page_obj"])
        cells = [cell.strip() for cell in EVENT_KEY_CELL.findall(response.content.decode())]
        self.assertEqual(len(cells), len(rows), "one 令牌 cell per request-event row")
        return rows, cells


class QueryTraceKeyIndexTests(KeyPanelTestMixin, TestCase):
    """The option list itself: union, dedup, deleted keys, blanks."""

    def test_index_unions_both_tables_and_deduplicates_shared_identifiers(self):
        self.trace("aaaa0001", IDENT_BOTH)
        self.usage_log("keeper-both-1", IDENT_BOTH)
        self.trace("aaaa0002", IDENT_ALPHA)
        self.usage_log("keeper-hist-1", IDENT_HISTORIC)
        self.usage_log("keeper-blank-1", "")

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")

        options = self.option_values(response)
        self.assertEqual(len(options), len(set(options)), "one option per distinct identifier")
        self.assertEqual(
            sorted(options),
            sorted(key_index(i) for i in (IDENT_BOTH, IDENT_ALPHA, IDENT_HISTORIC)),
        )
        # blank identifiers are not keys: they must not become a selectable option
        self.assertNotIn(key_index(""), options)

    def test_usage_log_only_history_is_still_selectable(self):
        """A key that only survives in UsageLog (no QueryTrace row at all)."""
        self.trace("aaaa0003", IDENT_ALPHA)
        self.usage_log("keeper-hist-2", IDENT_HISTORIC)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")

        self.assertIn(key_index(IDENT_HISTORIC), self.option_values(response))
        # selecting it is a valid, empty query rather than an "unknown filter" dead end
        selected = self.trace_list(range="all", key=key_index(IDENT_HISTORIC))
        self.assertEqual(selected.context["total_count"], 0)
        self.assertNotContains(selected, "aaaa0003")

    def test_deleted_key_still_appears_and_still_filters(self):
        member = User.objects.create_user(username="key-panel-deleted")
        api_key = APIKey.objects.create(
            user=member,
            name="legacy-key",
            token_hash="hash-legacy-key",
            token_prefix=IDENT_LEGACY,
            token_plaintext="labsk-deleted0001-plain",
        )
        self.trace("aaaa0004", IDENT_LEGACY, api_key=api_key, user=member)
        api_key.delete()

        trace = QueryTrace.objects.get(short_id="aaaa0004")
        self.assertIsNone(trace.api_key, "SET_NULL keeps the row")
        self.assertEqual(trace.api_key_identifier, IDENT_LEGACY)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")
        self.assertIn(key_index(IDENT_LEGACY), self.option_values(response))

        filtered = self.trace_list(range="all", key=key_index(IDENT_LEGACY))
        self.assertContains(filtered, "aaaa0004")

    def test_option_list_ignores_range_and_member_and_model_filters(self):
        """Options come from the whole retained history, not from the filtered queryset."""
        self.trace("aaaa0005", IDENT_ALPHA, user=self.member)
        self.trace("aaaa0006", IDENT_HISTORIC, user=None, model="gpt-5.6-terra")

        self.client.force_login(self.admin)
        response = self.trace_list(
            range="today",
            user=self.member.pk,
            model="gpt-5.6-terra",
        )

        self.assertEqual(response.context["total_count"], 0)
        options = self.option_values(response)
        self.assertIn(key_index(IDENT_ALPHA), options)
        self.assertIn(key_index(IDENT_HISTORIC), options)

    def test_key_index_helper_matches_the_view(self):
        self.trace("aaaa0007", IDENT_LONG)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")

        self.assertIn(key_index(IDENT_LONG), self.option_values(response))
        self.assertEqual(len(key_index(IDENT_LONG)), 64)

    def test_plain_member_cannot_reach_the_admin_key_panel(self):
        self.trace("aaaa0008", IDENT_ALPHA)

        self.client.force_login(self.member)
        response = self.client.get(reverse("admin-query-traces"))

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(IDENT_ALPHA, response.content.decode())


class QueryTraceKeyFilterTests(KeyPanelTestMixin, TestCase):
    """Filtering by a key index: exact match, invalid indexes, filter carry-over."""

    def test_two_identifiers_sharing_a_suffix_do_not_mix(self):
        self.trace("bbbb0001", IDENT_ALPHA)
        self.trace("bbbb0002", IDENT_BETA)

        self.client.force_login(self.admin)
        alpha = self.trace_list(range="all", key=key_index(IDENT_ALPHA))
        beta = self.trace_list(range="all", key=key_index(IDENT_BETA))

        self.assertContains(alpha, "bbbb0001")
        self.assertNotContains(alpha, "bbbb0002")
        self.assertContains(beta, "bbbb0002")
        self.assertNotContains(beta, "bbbb0001")

    def test_unknown_key_index_returns_an_empty_result(self):
        self.trace("bbbb0003", IDENT_ALPHA)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all", key="deadbeef")

        self.assertEqual(response.context["total_count"], 0)
        self.assertTrue(response.context["key_filter_unknown"])
        self.assertContains(response, "Key 筛选参数无效")
        self.assertNotContains(response, "bbbb0003")
        self.assertNotContains(response, IDENT_ALPHA)

    def test_selected_key_is_exact_and_case_sensitive(self):
        self.trace("bbbb0004", IDENT_ALPHA)
        self.trace("bbbb0005", IDENT_ALPHA.upper())

        self.client.force_login(self.admin)
        response = self.trace_list(range="all", key=key_index(IDENT_ALPHA))

        self.assertContains(response, "bbbb0004")
        self.assertNotContains(response, "bbbb0005")

    def test_pagination_links_keep_the_key_filter(self):
        for index in range(26):
            self.trace(f"cccc{index:04d}", IDENT_ALPHA)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all", key=key_index(IDENT_ALPHA))

        self.assertEqual(response.context["paginator"].num_pages, 2)
        token = key_index(IDENT_ALPHA)
        self.assertContains(response, f"?page=2&amp;range=all&amp;key={token}")
        self.assertContains(response, f"?range=today&amp;key={token}")

    def test_range_buttons_keep_the_key_filter(self):
        self.trace("bbbb0006", IDENT_ALPHA)

        self.client.force_login(self.admin)
        response = self.trace_list(range="today", key=key_index(IDENT_ALPHA))

        self.assertContains(response, f"?range=last_7_days&amp;key={key_index(IDENT_ALPHA)}")

    def test_custom_date_form_keeps_the_other_filters(self):
        member = User.objects.create_user(username="key-panel-custom")
        self.trace(
            "bbbb0007",
            IDENT_ALPHA,
            when=timezone.now() - timedelta(days=5),
            user=member,
            model="gpt-5.6-terra",
        )

        self.client.force_login(self.admin)
        token = key_index(IDENT_ALPHA)
        start = (timezone.localtime() - timedelta(days=7)).date().isoformat()
        end = timezone.localtime().date().isoformat()
        response = self.trace_list(
            range="custom",
            start=start,
            end=end,
            key=token,
            user=member.pk,
            model="gpt-5.6-terra",
            q="排查",
        )

        self.assertContains(response, "bbbb0007")
        # the custom-range form must carry every other filter as a hidden input
        self.assertContains(response, f'<input type="hidden" name="key" value="{token}">')
        self.assertContains(response, f'<input type="hidden" name="user" value="{member.pk}">')
        self.assertContains(response, '<input type="hidden" name="model" value="gpt-5.6-terra">')
        self.assertContains(response, '<input type="hidden" name="q" value="排查">')
        self.assertContains(response, f'name="start" value="{start}"')
        self.assertContains(response, f'name="end" value="{end}"')

    def test_key_plus_other_filters_narrows_instead_of_widening(self):
        self.trace("bbbb0008", IDENT_ALPHA, user=self.member)
        self.trace("bbbb0009", IDENT_ALPHA, user=self.admin)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all", key=key_index(IDENT_ALPHA), user=self.member.pk)

        self.assertContains(response, "bbbb0008")
        self.assertNotContains(response, "bbbb0009")


class QueryTraceKeyDisplayTests(KeyPanelTestMixin, TestCase):
    """Masked rendering on the list, on the detail page, and the 60-character preview."""

    def test_list_and_detail_show_at_most_five_characters_and_never_the_identifier(self):
        self.trace("dddd0001", IDENT_LONG, query_text="生产环境密钥排查")

        self.client.force_login(self.admin)
        listing = self.trace_list(range="all")
        detail = self.client.get(reverse("admin-query-trace-detail", args=["dddd0001"]))
        self.assertEqual(detail.status_code, 200)

        self.assertEqual(LABEL_LONG, "*nt01")
        self.assertEqual(len(LABEL_LONG), 5)
        self.assertEqual(self.trace_key_cells(listing), [LABEL_LONG])
        for response in (listing, detail):
            self.assertContains(response, LABEL_LONG)
            self.assertNotContains(response, IDENT_LONG)

    def test_option_labels_are_bounded_and_collisions_get_an_ordinal(self):
        self.trace("dddd0002", IDENT_ALPHA)
        self.trace("dddd0003", IDENT_BETA)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")

        alpha = self.option_for(response, IDENT_ALPHA)
        beta = self.option_for(response, IDENT_BETA)
        self.assertEqual(alpha["label"], "*zzz1")
        self.assertEqual(beta["label"], "*zzz1")
        self.assertLessEqual(len(alpha["label"]), 5)
        # both collide on the suffix, so both carry an ordinal and are distinguishable
        self.assertIsNotNone(alpha["ordinal"])
        self.assertIsNotNone(beta["ordinal"])
        self.assertNotEqual(alpha["ordinal"], beta["ordinal"])
        self.assertContains(response, "· #1")
        self.assertContains(response, "· #2")
        self.assertNotContains(response, IDENT_ALPHA)
        self.assertNotContains(response, IDENT_BETA)

    def test_unique_suffix_needs_no_ordinal(self):
        self.trace("dddd0004", IDENT_LONG)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")

        option = self.option_for(response, IDENT_LONG)
        self.assertIsNone(option["ordinal"])
        self.assertNotContains(response, "· #")

    def test_short_identifier_is_shown_with_the_star_prefix(self):
        self.trace("dddd0005", IDENT_SHORT)

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")

        self.assertEqual(key_suffix(IDENT_SHORT, 4), "*bsk7")
        self.assertEqual(self.trace_key_cells(response), ["*bsk7"])
        self.assertNotContains(response, IDENT_SHORT)

    def test_trace_without_any_identifier_shows_a_dash_and_no_option(self):
        self.trace("dddd0006", "")

        self.client.force_login(self.admin)
        response = self.trace_list(range="all")

        self.assertContains(response, "dddd0006")
        self.assertEqual(self.trace_key_cells(response), ["-"])
        self.assertEqual(response.context["key_options"], [])
        self.assertNotIn(key_index(""), self.option_values(response))

    def test_list_preview_is_capped_at_sixty_characters_while_detail_shows_everything(self):
        sixty = "字" * 60
        long_text = "长" * 200
        self.trace("dddd0007", IDENT_LONG, query_text=sixty)
        self.trace("dddd0008", IDENT_LONG, query_text=long_text)

        self.client.force_login(self.admin)
        listing = self.trace_list(range="all")

        # exactly 60 characters: nothing is cut, so no ellipsis is added
        self.assertContains(listing, sixty)
        self.assertNotContains(listing, "字" * 57 + "...")
        # 200 characters: truncatechars:60 keeps 57 characters plus the "..." suffix
        # (the whole rendered preview is 60 characters, never more)
        self.assertContains(listing, "长" * 57 + "...")
        self.assertNotContains(listing, "长" * 58)
        self.assertNotContains(listing, long_text)

        detail = self.client.get(reverse("admin-query-trace-detail", args=["dddd0008"]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, long_text)


class RequestEventsKeyMaskingTests(KeyPanelTestMixin, TestCase):
    """``*`` + last 10 characters, identical for admins and plain members."""

    def request_events(self):
        with patch("usage.views.auto_sync_usage_queue", return_value=({}, False)) as queue:
            response = self.client.get(reverse("request-events"))
            queue.assert_called_once()
        self.assertEqual(response.status_code, 200)
        return response

    def test_admin_and_member_see_the_same_masked_key(self):
        self.usage_log("keeper-mask-admin", IDENT_LONG, user=self.admin)
        self.usage_log("keeper-mask-member", IDENT_LONG, user=self.member)
        masked = key_suffix(IDENT_LONG, 10)
        self.assertEqual(masked, EVENT_LABEL_LONG)
        self.assertEqual(masked, "*ongident01")
        self.assertEqual(len(masked), 11)

        self.client.force_login(self.admin)
        admin_view = self.request_events()
        rows, cells = self.event_key_cells(admin_view)
        self.assertEqual(set(cells), {masked})
        self.assertEqual(len(rows), 2)
        self.assertNotContains(admin_view, IDENT_LONG)

        self.client.force_login(self.member)
        member_view = self.request_events()
        rows, cells = self.event_key_cells(member_view)
        self.assertEqual(set(cells), {masked})
        self.assertEqual(len(rows), 2)
        self.assertNotContains(member_view, IDENT_LONG)

    def test_long_identifier_keeps_only_the_last_ten_characters(self):
        identifier = "labsk-abcdefghij12"
        self.usage_log("keeper-mask-long", identifier, user=self.admin)

        self.client.force_login(self.admin)
        response = self.request_events()

        self.assertEqual(key_suffix(identifier, 10), "*cdefghij12")
        rows, cells = self.event_key_cells(response)
        self.assertEqual(cells, ["*cdefghij12"])
        self.assertEqual(rows[0].api_key_identifier, identifier)
        self.assertNotContains(response, identifier)

    def test_identifier_shorter_than_the_suffix_is_shown_in_full_behind_the_star(self):
        self.usage_log("keeper-mask-short", IDENT_SHORT, user=self.admin)

        self.client.force_login(self.admin)
        response = self.request_events()

        self.assertEqual(key_suffix(IDENT_SHORT, 10), "*labsk7")
        rows, cells = self.event_key_cells(response)
        self.assertEqual(cells, ["*labsk7"])

    def test_unknown_blank_identifier_renders_a_dash(self):
        self.usage_log("keeper-mask-blank", "", user=self.admin)

        self.client.force_login(self.admin)
        response = self.request_events()

        rows, cells = self.event_key_cells(response)
        # the row is still listed; only its key column degrades to a placeholder
        self.assertEqual([row.request_id for row in rows], ["keeper-mask-blank"])
        self.assertEqual(rows[0].api_key_identifier, "")
        self.assertEqual(cells, ["-"])

    def test_no_identifier_reaches_the_page_or_its_url(self):
        self.usage_log("keeper-mask-leak", IDENT_BOTH, user=self.admin)

        self.client.force_login(self.admin)
        response = self.request_events()

        rows, cells = self.event_key_cells(response)
        self.assertEqual(cells, ["*both000001"])
        self.assertNotContains(response, IDENT_BOTH)
        self.assertNotContains(response, key_index(IDENT_BOTH))
        for identifier in IDENTIFIERS:
            self.assertNotContains(response, identifier)
