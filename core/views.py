from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from core.forms import AnnouncementForm, DiscussionPostForm, DiscussionTopicForm
from core.models import Announcement, DiscussionPost, DiscussionTopic
from core.utils import admin_required


def _is_admin(user):
    return user.is_staff or getattr(getattr(user, "profile", None), "role", None) == "admin"


def _topics():
    return DiscussionTopic.objects.select_related("created_by__profile").annotate(post_count=Count("posts")).order_by("-updated_at", "-pk")


def _page(request, queryset, size=20):
    return Paginator(queryset, size).get_page(request.GET.get("page"))


def _detail_context(request, topic, form=None):
    posts = topic.posts.select_related("author__profile").order_by("created_at", "pk")
    return {"topic": topic, "page_obj": _page(request, posts),
            "form": form if form is not None else DiscussionPostForm(),
            "can_edit_topic": _is_admin(request.user) or topic.created_by_id == request.user.pk}


@login_required
@require_http_methods(["GET", "HEAD"])
def home(request):
    announcements = Announcement.objects.filter(is_published=True).order_by("-created_at", "-pk")[:5]
    return render(request, "core/home.html", {"announcements": announcements, "discussion_topics": _topics()[:8]})


@login_required
@require_http_methods(["GET", "HEAD"])
def announcement_public_list(request):
    announcements = Announcement.objects.filter(is_published=True).order_by("-created_at", "-pk")
    return render(request, "core/announcement_list.html", {"page_obj": _page(request, announcements)})


@login_required
@require_http_methods(["GET", "HEAD"])
def discussion_list(request):
    return render(request, "core/discussion_list.html", {"page_obj": _page(request, _topics())})


@login_required
@require_http_methods(["GET", "HEAD"])
def discussion_detail(request, topic_id):
    topic = get_object_or_404(DiscussionTopic.objects.select_related("created_by__profile"), pk=topic_id)
    return render(request, "core/discussion_detail.html", _detail_context(request, topic))


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def discussion_create(request):
    data = request.POST if request.method == "POST" else None
    topic_form, post_form = DiscussionTopicForm(data), DiscussionPostForm(data)
    if request.method == "POST" and topic_form.is_valid() and post_form.is_valid():
        topic = topic_form.save(commit=False)
        topic.created_by = request.user
        topic.save()
        post = post_form.save(commit=False)
        post.topic, post.author = topic, request.user
        post.save()
        return redirect("discussion-detail", topic_id=topic.pk)
    return render(request, "core/discussion_form.html", {"topic_form": topic_form, "post_form": post_form})


@login_required
@require_POST
@transaction.atomic
def discussion_reply(request, topic_id):
    topic = get_object_or_404(DiscussionTopic.objects.select_for_update(), pk=topic_id)
    form = DiscussionPostForm(request.POST)
    if form.is_valid():
        post = form.save(commit=False)
        post.topic, post.author = topic, request.user
        post.save()
        topic.save(update_fields=["updated_at"])
        page = (topic.posts.count() - 1) // 20 + 1
        from django.urls import reverse
        return redirect(f"{reverse('discussion-detail', args=[topic.pk])}?page={page}#post-{post.pk}")
    return render(request, "core/discussion_detail.html", _detail_context(request, topic, form), status=400)


def _check_post_permission(user, post):
    if not (_is_admin(user) or post.author_id == user.pk):
        raise PermissionDenied("只能管理自己的帖子")


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def discussion_edit(request, post_id):
    post = get_object_or_404(DiscussionPost.objects.select_related("topic"), pk=post_id)
    _check_post_permission(request.user, post)
    form = DiscussionPostForm(request.POST if request.method == "POST" else None, instance=post)
    if request.method == "POST" and form.is_valid():
        form.save()
        DiscussionTopic.objects.filter(pk=post.topic_id).update(updated_at=timezone.now())
        return redirect("discussion-detail", topic_id=post.topic_id)
    return render(request, "core/discussion_edit.html", {"form": form, "post": post})


@login_required
@require_POST
@transaction.atomic
def discussion_delete(request, post_id):
    post = get_object_or_404(DiscussionPost, pk=post_id)
    _check_post_permission(request.user, post)
    topic = get_object_or_404(DiscussionTopic.objects.select_for_update(), pk=post.topic_id)
    post.delete()
    # Removing one's post must never cascade into other members' replies.
    if not topic.posts.exists():
        topic.delete()
        return redirect("discussion-list")
    topic.save(update_fields=["updated_at"])
    messages.success(request, "帖子已删除，其他成员的回复已保留。")
    return redirect("discussion-detail", topic_id=topic.pk)


@login_required
@require_http_methods(["GET", "POST"])
def discussion_topic_edit(request, topic_id):
    topic = get_object_or_404(DiscussionTopic, pk=topic_id)
    if not (_is_admin(request.user) or topic.created_by_id == request.user.pk):
        raise PermissionDenied("只能编辑自己的主题")
    form = DiscussionTopicForm(request.POST if request.method == "POST" else None, instance=topic)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect("discussion-detail", topic_id=topic.pk)
    return render(request, "core/topic_edit.html", {"form": form, "topic": topic})


@admin_required
@require_POST
def discussion_topic_delete(request, topic_id):
    get_object_or_404(DiscussionTopic, pk=topic_id).delete()
    return redirect("discussion-list")


@admin_required
@require_http_methods(["GET", "HEAD"])
def announcement_list(request):
    return render(request, "core/announcement_admin_list.html", {"page_obj": _page(request, Announcement.objects.order_by("-created_at", "-pk"))})


@admin_required
@require_http_methods(["GET", "POST"])
def announcement_create(request):
    form = AnnouncementForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "公告已创建。")
        return redirect("announcement-admin-list")
    return render(request, "core/announcement_form.html", {"form": form, "heading": "新建公告"})


@admin_required
@require_http_methods(["GET", "POST"])
def announcement_edit(request, announcement_id):
    announcement = get_object_or_404(Announcement, pk=announcement_id)
    form = AnnouncementForm(request.POST if request.method == "POST" else None, instance=announcement)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "公告已更新。")
        return redirect("announcement-admin-list")
    return render(request, "core/announcement_form.html", {"form": form, "heading": "编辑公告"})


@admin_required
@require_POST
def announcement_delete(request, announcement_id):
    get_object_or_404(Announcement, pk=announcement_id).delete()
    messages.success(request, "公告已删除。")
    return redirect("announcement-admin-list")


def health_check(request):
    """Minimal public health probe, without database exception disclosure."""
    from django.db import connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "error"}, status=503)
