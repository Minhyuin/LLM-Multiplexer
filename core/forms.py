from django import forms
from django.conf import settings

from core.attachments import check_batch_limits, human_size, validate_attachment
from core.models import Announcement, DiscussionPost, DiscussionTopic

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
