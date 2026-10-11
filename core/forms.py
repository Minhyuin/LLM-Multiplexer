from django import forms

from core.models import Announcement, DiscussionPost, DiscussionTopic


class PortalForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"


class AnnouncementForm(PortalForm):
    body = forms.CharField(label="公告正文", max_length=20000, widget=forms.Textarea(attrs={"rows": 8}))

    class Meta:
        model = Announcement
        fields = ["title", "body", "is_published"]
        labels = {"title": "公告标题", "is_published": "发布公告（取消勾选即下线）"}


class DiscussionTopicForm(PortalForm):
    class Meta:
        model = DiscussionTopic
        fields = ["title"]
        labels = {"title": "讨论标题"}


class DiscussionPostForm(PortalForm):
    body = forms.CharField(label="内容", max_length=10000, widget=forms.Textarea(attrs={"rows": 6}))

    class Meta:
        model = DiscussionPost
        fields = ["body"]
