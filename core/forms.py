from django import forms
from django.conf import settings

from core.attachments import check_batch_limits, human_size, validate_attachment
from core.models import Announcement, DiscussionPost, DiscussionTopic, SiteSettings
from core.site_settings import normalize_orphan_lab_groups

ATTACHMENT_HELP = (
    "可一次选择多个文件（按住 Ctrl/⌘ 多选）。"
    f"每条内容最多 {settings.ATTACHMENT_MAX_PER_CONTENT} 个附件，"
    f"单个不超过 {human_size(settings.ATTACHMENT_MAX_BYTES)}，"
    f"本次合计不超过 {human_size(settings.ATTACHMENT_MAX_BATCH_BYTES)}。"
    "允许：图片 jpg / jpeg / png / gif / webp；"
    "文档 pdf / doc(x) / xls(x) / ppt(x) / odt / ods / odp / rtf；"
    "文本 txt / md / log / json / csv / yaml / ini / xml；"
    "压缩包 zip / tar / gz / tgz。"
    "可执行文件、脚本与 SVG/HTML 会被拒绝。"
)


class MultipleFileInput(forms.FileInput):
    """File input that accepts a multi-file selection (Django's default does not)."""

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """``FileField`` that accepts a list of uploads.

    ``FileInput.value_from_datadict`` hands back ``files.getlist(name)`` when
    ``allow_multiple_selected`` is set, but the stock ``FileField`` would run
    ``to_python`` on that list and reject it ("invalid").  Cleaning each item
    separately is the pattern Django's own docs recommend for multi-upload.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(item, initial) for item in data if item]
        return single(data, initial)


class PortalForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"


def attachment_field():
    """Build a fresh ``attachments`` field (declared on each concrete form)."""
    return MultipleFileField(
        label="附件",
        required=False,
        widget=MultipleFileInput(attrs={"multiple": True}),
        help_text=ATTACHMENT_HELP,
    )


class AttachmentUploadMixin:
    """Validation helpers for the shared multi-file ``attachments`` field.

    Note: the field itself is declared on every concrete form, *not* here.  A
    plain mixin never goes through ``DeclarativeFieldsMetaclass``, so it has no
    ``declared_fields`` and a field declared on it would be silently dropped
    from ``form.fields``.  Every upload is validated up front (count, size,
    aggregate size, extension, decoded image), so a rejected upload never leaves
    a partial write behind.
    """

    def _existing_attachment_count(self) -> int:
        instance = getattr(self, "instance", None)
        if instance is not None and instance.pk:
            return instance.attachments.count()
        return 0

    def clean_attachments(self):
        uploads = self.files.getlist("attachments")
        if not uploads:
            return []
        check_batch_limits(uploads, self._existing_attachment_count())
        for upload in uploads:
            validate_attachment(upload)
        return uploads


class AnnouncementForm(AttachmentUploadMixin, PortalForm):
    body = forms.CharField(label="公告正文", max_length=20000, widget=forms.Textarea(attrs={"rows": 8}))
    attachments = attachment_field()

    class Meta:
        model = Announcement
        fields = ["title", "body", "is_published"]
        labels = {"title": "公告标题", "is_published": "发布公告（取消勾选即下线）"}


class DiscussionTopicForm(PortalForm):
    class Meta:
        model = DiscussionTopic
        fields = ["title"]
        labels = {"title": "讨论标题"}


class DiscussionPostForm(AttachmentUploadMixin, PortalForm):
    body = forms.CharField(label="内容", max_length=10000, widget=forms.Textarea(attrs={"rows": 6}))
    attachments = attachment_field()

    class Meta:
        model = DiscussionPost
        fields = ["body"]


class SiteSettingsForm(PortalForm):
    query_trace_enabled = forms.TypedChoiceField(
        label="Query 追溯",
        choices=(("", "跟随环境变量"), ("1", "开启"), ("0", "关闭")),
        coerce=lambda value: value == "1",
        required=False,
        help_text="控制门户是否读取 CPA 请求日志并记录用户提问；需 CPA 已开启 request-log。",
    )
    fingerprint_enabled = forms.TypedChoiceField(
        label="指纹检测",
        choices=(("", "跟随环境变量"), ("1", "开启"), ("0", "关闭")),
        coerce=lambda value: value == "1",
        required=False,
        help_text="控制检测入口和后台 worker 是否接单；关闭后历史记录仍保留。",
    )
    fingerprint_daily_limit = forms.IntegerField(
        label="普通成员每日检测次数", min_value=1, max_value=1000, required=False,
        help_text="留空跟随环境变量 FINGERPRINT_DAILY_LIMIT。",
    )
    fingerprint_admin_daily_limit = forms.IntegerField(
        label="管理员每日检测次数", min_value=1, max_value=1000, required=False,
        help_text="留空跟随环境变量 FINGERPRINT_ADMIN_DAILY_LIMIT。",
    )
    fingerprint_cooldown_seconds = forms.IntegerField(
        label="同一用户检测冷却（秒）", min_value=1, max_value=86400, required=False,
        help_text="留空跟随环境变量 FINGERPRINT_COOLDOWN_SECONDS。",
    )
    fingerprint_concurrency = forms.IntegerField(
        label="检测并发数", min_value=1, max_value=4, required=False,
        help_text="留空跟随环境变量 FINGERPRINT_CONCURRENCY；生产机建议 1。",
    )
    lab_groups = forms.CharField(
        label="用户分组类型",
        required=False,
        max_length=5000,
        widget=forms.Textarea(attrs={"rows": 5, "placeholder": "电控\n算法\n机械\n宣传"}),
        help_text="每行一个分组名称，最多 20 个、每个不超过 100 字；留空使用内置四种。",
    )

    class Meta:
        model = SiteSettings
        fields = [
            "public_api_base_url",
            "query_trace_enabled",
            "fingerprint_enabled",
            "fingerprint_daily_limit",
            "fingerprint_admin_daily_limit",
            "fingerprint_cooldown_seconds",
            "fingerprint_concurrency",
            "lab_groups",
        ]
        labels = {"public_api_base_url": "公开 API 根地址"}
        widgets = {
            "public_api_base_url": forms.URLInput(
                attrs={"placeholder": "https://api.example.com", "autocomplete": "url"}
            )
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("query_trace_enabled", "fingerprint_enabled"):
            value = getattr(self.instance, name, None)
            self.fields[name].initial = "" if value is None else ("1" if value else "0")
            self.fields[name].widget.attrs["class"] = "form-select"

    def clean_query_trace_enabled(self):
        value = self.cleaned_data.get("query_trace_enabled")
        return None if value in (None, "") else bool(value)

    def clean_fingerprint_enabled(self):
        value = self.cleaned_data.get("fingerprint_enabled")
        return None if value in (None, "") else bool(value)

    def clean_lab_groups(self):
        names = []
        for line in (self.cleaned_data.get("lab_groups") or "").splitlines():
            name = line.strip()
            if not name:
                continue
            if len(name) > 100:
                raise forms.ValidationError("每个分组名称不能超过 100 个字符。")
            if name not in names:
                names.append(name)
        if len(names) > 20:
            raise forms.ValidationError("最多配置 20 个用户分组。")
        return "\n".join(names)

    def save(self, commit=True):
        obj = super().save(commit=commit)
        if commit:
            normalize_orphan_lab_groups()
        return obj
