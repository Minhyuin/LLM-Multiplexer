"""Shared fingerprint results board.

Every signed-in member may read the results other members produced; only portal
admins may delete a row. The page is server-rendered and read-only: it never
admits, expires or requeues a task.
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.utils import admin_required, is_portal_admin
from users.middleware import get_real_user

from .config import enabled, limit
from .models import FingerprintRun
from .services import owner_display_name

PAGE_SIZE = 40

# The detector reports one of these; anything else is shown as-is rather than
# guessed at, so a new upstream verdict cannot silently render as "scored".
VERDICT_LABELS = {
    "scored": "参考排名可用",
    "partial": "部分可用",
    "unscorable": "无法判定",
}


def retention_hours():
    return limit("HISTORY_HOURS", 168)


def _candidates(result):
    rows = result.get("candidates") if isinstance(result, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)][:3]


def _rows(runs):
    rows = []
    for run in runs:
        result = run.result if isinstance(run.result, dict) else {}
        verdict = (result.get("verdict") or "").strip()
        rows.append({
            "id": run.pk,
            "member": owner_display_name(run.user),
            "model_id": run.model_id,
            "status": run.status,
            "status_label": run.get_status_display(),
            "finished_at": run.finished_at,
            "family": (result.get("family") or result.get("prediction") or "").strip(),
            "verdict": verdict,
            "verdict_label": VERDICT_LABELS.get(verdict, verdict),
            "candidates": _candidates(result),
            "error_message": run.error_message,
        })
    return rows


@login_required
def board(request):
    hours = retention_hours()
    since = timezone.now() - timedelta(hours=hours)
    queryset = (FingerprintRun.objects.exclude(status__in=FingerprintRun.ACTIVE)
                .filter(finished_at__gte=since)
                .select_related("user", "user__profile")
                .order_by("-finished_at", "-created_at"))
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page") or 1)
    return render(request, "dashboard/fingerprint_board.html", {
        "rows": _rows(page.object_list),
        "page_obj": page,
        "total": page.paginator.count,
        "retention_days": max(1, round(hours / 24)),
        "fingerprint_enabled": enabled(),
        "can_delete": is_portal_admin(get_real_user(request)),
    })


@admin_required
@require_POST
def board_delete(request, run_id):
    run = get_object_or_404(FingerprintRun, pk=run_id)
    if run.status in FingerprintRun.ACTIVE:
        messages.error(request, "进行中的检测不能从看板删除。")
    else:
        model_id = run.model_id
        run.delete()
        messages.success(request, f"已删除 {model_id} 的检测结果。")
    page = (request.POST.get("page") or "").strip()
    target = reverse("fingerprint-board")
    return redirect(f"{target}?page={page}" if page.isdigit() else target)
