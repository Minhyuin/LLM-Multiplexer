"""Forms for invitation-code self-registration.

``RegistrationForm`` only exposes the six registration fields; a submitted
``is_staff``/``is_superuser``/``role`` never reaches the created account.
``InvitationCodeForm`` is the admin-facing creation form: a blank code is
replaced with a random 24-character uppercase-alphanumeric code, and the
creator is assigned by the view (``form.save(commit=False)`` -> set
``created_by`` -> ``save()``).
"""
import re

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.utils import timezone

from core.site_settings import lab_group_choices
from users.models import (
    INVITATION_CODE_MAX_LENGTH,
    INVITATION_CODE_PATTERN,
    INVITATION_CODE_VALID_DAYS_MAX,
    INVITATION_CODE_VALID_DAYS_MIN,
    InvitationCode,
)
from users.registration import (
    username_from_display_name,
    username_matches_display_name,
    unique_invitation_code,
)

User = get_user_model()

_HAN_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3007]")
_DISPLAY_NAME_RE = re.compile(r"^[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3007·\s]{2,50}$")
_USERNAME_RE = re.compile(r"^[A-Za-z]{2,64}$")
_INVITATION_CODE_RE = re.compile(INVITATION_CODE_PATTERN)


class InvitationCodeForm(forms.ModelForm):
    """Create an invitation code.  ``created_by`` is set by the caller."""

    class Meta:
        model = InvitationCode
        fields = ["code", "valid_days"]
        widgets = {
            "code": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "off",
                    "placeholder": "留空自动生成",
                }
            ),
            "valid_days": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": INVITATION_CODE_VALID_DAYS_MIN,
                    "max": INVITATION_CODE_VALID_DAYS_MAX,
                    "autocomplete": "off",
                }
            ),
        }
        help_texts = {
            "code": "仅限 ASCII 字母、数字、_ 和 -，最长 64 位；留空则随机生成 24 位大写字母数字。",
            "valid_days": "整数 1-7 天；到期时间在创建时固定，之后不能延长。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A blank code means "generate one in clean_code()".
        self.fields["code"].required = False
        if not self.is_bound:
            self.fields["valid_days"].initial = INVITATION_CODE_VALID_DAYS_MAX

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip()
        if not code:
            # ModelForm's unique check runs after this, but generating a
            # database-unique value here keeps validation errors rare.
            return unique_invitation_code()
        return code


class RegistrationForm(forms.Form):
    """Public registration form: Chinese name + pinyin username + invitation."""

    display_name = forms.CharField(
        label="中文姓名",
        max_length=50,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "name",
                "placeholder": "例如：张三",
            }
        ),
    )
    username = forms.CharField(
        label="用户名（拼音）",
        max_length=64,
        required=False,
        help_text="默认按中文姓名自动生成，例如 ZhangSan；多音字可自行调整。",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "username",
                "placeholder": "例如：ZhangSan",
            }
        ),
    )
    invitation_code = forms.CharField(
        label="邀请码",
        max_length=INVITATION_CODE_MAX_LENGTH,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "autocomplete": "off",
                "placeholder": "请输入管理员提供的邀请码",
            }
        ),
    )
    lab_group = forms.ChoiceField(
        label="实验室分组",
        choices=(),
        help_text="请选择你所在的分组；管理员后续调整分组时，原分组失效的账户会进入“未分组”。",
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    password1 = forms.CharField(
        label="密码",
        strip=False,
        help_text="请使用不易猜测的密码。",
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "autocomplete": "new-password",
            }
        ),
    )
    password2 = forms.CharField(
        label="确认密码",
        strip=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "form-control",
                "autocomplete": "new-password",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lab_group"].choices = lab_group_choices()

    def clean_display_name(self):
        value = re.sub(r"\s+", " ", (self.cleaned_data.get("display_name") or "").strip())
        if not _DISPLAY_NAME_RE.fullmatch(value) or not _HAN_RE.search(value):
            raise ValidationError("请输入 2-50 位中文姓名（可含间隔号 ·）")
        return value

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if not username:
            # Left blank on purpose: clean() derives it from display_name.
            return ""
        if not _USERNAME_RE.fullmatch(username):
            raise ValidationError("用户名须为 2-64 位 ASCII 拼音字母，每字首字母大写，例如 ZhangSan")
        display_name = self.cleaned_data.get("display_name")
        if display_name and not username_matches_display_name(display_name, username):
            raise ValidationError("用户名须与中文姓名对应（拼音逐字首字母大写）；多音字可使用其他合理读音")
        return username

    def clean_invitation_code(self):
        code = (self.cleaned_data.get("invitation_code") or "").strip()
        if not code:
            raise ValidationError("请输入邀请码")
        if not _INVITATION_CODE_RE.fullmatch(code):
            raise ValidationError("邀请码格式不正确")
        invitation = InvitationCode.objects.filter(code=code).first()
        if invitation is None:
            raise ValidationError("邀请码无效，请检查后重试")
        if not invitation.is_active:
            raise ValidationError("邀请码已停用")
        if not invitation.expires_at or invitation.expires_at <= timezone.now():
            raise ValidationError("邀请码已过期")
        self.invitation = invitation
        return code

    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get("username") or ""
        display_name = cleaned_data.get("display_name") or ""

        if not username and display_name and "username" not in self.errors:
            try:
                username = username_from_display_name(display_name)
            except ValueError as exc:
                self.add_error("username", str(exc))
            else:
                if not _USERNAME_RE.fullmatch(username):
                    self.add_error(
                        "username",
                        "自动生成的用户名须为 2-64 位 ASCII 拼音字母，请缩短中文姓名或手动填写用户名",
                    )
                    username = ""
                else:
                    cleaned_data["username"] = username

        if username and User.objects.filter(username__iexact=username).exists():
            self.add_error("username", "该用户名已被使用，请更换")

        password1 = cleaned_data.get("password1") or ""
        password2 = cleaned_data.get("password2") or ""
        if password1 and password2 and password1 != password2:
            self.add_error("password2", "两次输入的密码不一致")
        elif password1 and "password1" not in self.errors:
            try:
                password_validation.validate_password(password1, user=User(username=username or "member"))
            except ValidationError as exc:
                self.add_error("password1", exc)

        return cleaned_data
