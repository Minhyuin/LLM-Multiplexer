from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, URLValidator
from django.db import models

from core.attachments import PrivateAttachmentStorage

attachment_storage = PrivateAttachmentStorage()


class Announcement(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title


class DiscussionTopic(models.Model):
    title = models.CharField(max_length=200)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="discussion_topics")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title


class DiscussionPost(models.Model):
    topic = models.ForeignKey(DiscussionTopic, on_delete=models.CASCADE, related_name="posts")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="discussion_posts")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author} - {self.topic}"


class Attachment(models.Model):
    """A member-uploaded file attached to an announcement or a discussion post.

    The blob lives under ``settings.PRIVATE_UPLOAD_ROOT`` (never MEDIA_ROOT) and is
    only ever streamed by the permission-checked ``attachment-serve`` view.
    """

    KIND_IMAGE = "image"
    KIND_DOCUMENT = "document"
    KIND_ARCHIVE = "archive"
    KIND_CHOICES = [(KIND_IMAGE, "图片"), (KIND_DOCUMENT, "文档"), (KIND_ARCHIVE, "压缩包")]

    announcement = models.ForeignKey(
        Announcement, on_delete=models.CASCADE, related_name="attachments", null=True, blank=True
    )
    post = models.ForeignKey(
        DiscussionPost, on_delete=models.CASCADE, related_name="attachments", null=True, blank=True
    )
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="attachments",
        null=True,
        blank=True,
    )
    file = models.FileField(upload_to="", storage=attachment_storage, max_length=255)
    original_name = models.CharField(max_length=255)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_DOCUMENT)
    mime_type = models.CharField(max_length=128, default="application/octet-stream")
    size = models.PositiveBigIntegerField(default=0)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(announcement__isnull=False, post__isnull=True)
                    | models.Q(announcement__isnull=True, post__isnull=False)
                ),
                name="attachment_exactly_one_parent",
            )
        ]

    def __str__(self):
        return self.original_name

    @property
    def parent(self):
        return self.announcement or self.post

    @property
    def is_image(self) -> bool:
        return self.kind == self.KIND_IMAGE

    def can_serve_inline(self) -> bool:
        """Only raster images may be shown inline; everything else is downloaded."""
        return self.is_image and self.mime_type.startswith("image/")


class SiteSettings(models.Model):
    """Admin-editable site settings that must change without a redeploy.

    There is exactly one row (pk=1).  ``public_api_base_url`` is optional: when
    it is blank the portal derives the public address from the incoming request,
    so a fresh deployment is correct without any manual configuration.
    """

    public_api_base_url = models.CharField(
        "公开 API 根地址",
        max_length=200,
        blank=True,
        help_text="例如 https://api.example.com；留空时自动使用成员访问门户的域名。",
    )
    query_trace_enabled = models.BooleanField(
        "Query 追溯",
        null=True,
        blank=True,
        default=None,
        help_text="留空时跟随环境变量 QUERY_TRACE_ENABLED。",
    )
    fingerprint_enabled = models.BooleanField(
        "指纹检测",
        null=True,
        blank=True,
        default=None,
        help_text="留空时跟随环境变量 FINGERPRINT_ENABLED。",
    )
    fingerprint_daily_limit = models.PositiveSmallIntegerField(
        "普通成员每日检测次数",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
        help_text="留空时跟随环境变量 FINGERPRINT_DAILY_LIMIT。",
    )
    fingerprint_admin_daily_limit = models.PositiveSmallIntegerField(
        "管理员每日检测次数",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
        help_text="留空时跟随环境变量 FINGERPRINT_ADMIN_DAILY_LIMIT。",
    )
    fingerprint_cooldown_seconds = models.PositiveIntegerField(
        "同一用户检测冷却（秒）",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(86400)],
        help_text="留空时跟随环境变量 FINGERPRINT_COOLDOWN_SECONDS。",
    )
    fingerprint_concurrency = models.PositiveSmallIntegerField(
        "检测并发数",
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(4)],
        help_text="留空时跟随环境变量 FINGERPRINT_CONCURRENCY。",
    )
    lab_groups = models.TextField(
        "用户分组类型",
        blank=True,
        default="",
        help_text="每行一个分组名称，最多 20 个；留空时使用内置的 电控/算法/机械/宣传。",
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="最后修改人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "站点设置"
        verbose_name_plural = "站点设置"

    def __str__(self):
        return "站点设置"

    def save(self, *args, **kwargs):
        # Enforce the singleton even if a caller passes another pk.
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def clean(self):
        super().clean()
        value = (self.public_api_base_url or "").strip()
        if not value:
            self.public_api_base_url = ""
            return

        try:
            URLValidator(schemes=["http", "https"])(value)
        except ValidationError as exc:
            raise ValidationError(
                {"public_api_base_url": "请输入以 http:// 或 https:// 开头的完整地址。"}
            ) from exc

        parsed = urlsplit(value)
        if not parsed.hostname:
            raise ValidationError({"public_api_base_url": "地址必须包含域名或 IP。"})
        if parsed.username or parsed.password:
            raise ValidationError({"public_api_base_url": "地址中不能包含用户名或密码。"})
        if parsed.query or parsed.fragment:
            raise ValidationError({"public_api_base_url": "地址中不能包含查询参数或锚点。"})
        if parsed.path.rstrip("/") not in {"", "/v1"}:
            raise ValidationError(
                {"public_api_base_url": "只填写站点根地址（可带 /v1），不要包含其他路径。"}
            )

        # Store a canonical origin; the portal appends /v1 where needed.
        self.public_api_base_url = f"{parsed.scheme}://{parsed.netloc}"
