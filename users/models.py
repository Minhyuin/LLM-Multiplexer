from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

INVITATION_CODE_MAX_LENGTH = 64
INVITATION_CODE_VALID_DAYS_MIN = 1
INVITATION_CODE_VALID_DAYS_MAX = 7
INVITATION_CODE_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

invitation_code_validator = RegexValidator(
    regex=INVITATION_CODE_PATTERN,
    message="邀请码只能包含 ASCII 字母、数字、下划线和短横线，长度 1-64 位",
)


class Profile(models.Model):
    GROUP_MECH = "机械"
    GROUP_ALGO = "算法"
    GROUP_CONTROL = "电控"
    GROUP_MEDIA = "宣传"
    GROUP_CHOICES = [
        (GROUP_CONTROL, "电控"),
        (GROUP_ALGO, "算法"),
        (GROUP_MECH, "机械"),
        (GROUP_MEDIA, "宣传"),
    ]

    MEMBER_CORE = "正式队员"
    MEMBER_RESERVE = "梯队队员"
    MEMBER_TYPE_CHOICES = [
        (MEMBER_CORE, "正式队员"),
        (MEMBER_RESERVE, "梯队队员"),
    ]

    ROLE_USER = "user"
    ROLE_ADMIN = "admin"
    ROLE_CHOICES = [
        (ROLE_USER, "普通用户"),
        (ROLE_ADMIN, "管理员"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    display_name = models.CharField(max_length=150, blank=True)
    email = models.EmailField(blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_USER)
    lab_group = models.CharField(max_length=100, blank=True, choices=GROUP_CHOICES)
    member_type = models.CharField(max_length=30, blank=True, choices=MEMBER_TYPE_CHOICES, default=MEMBER_CORE)
    grade = models.CharField(max_length=10, blank=True)
    is_dashboard_visible = models.BooleanField(default=True)
    monthly_token_quota = models.PositiveBigIntegerField(default=0)
    monthly_cost_quota = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]
        indexes = [
            models.Index(fields=["lab_group"]),
            models.Index(fields=["grade"]),
            models.Index(fields=["is_dashboard_visible"]),
        ]

    def __str__(self):
        return self.display_name or self.user.username


class InvitationCode(models.Model):
    """An invitation code usable by several people until it expires.

    ``valid_days`` and the resulting ``expires_at`` are fixed at creation time:
    an invitation can be stopped early through ``is_active`` but its lifetime
    can never be extended afterwards, so the 7-day ceiling cannot be bypassed
    by editing an existing row.
    """

    code = models.CharField(
        "邀请码",
        max_length=INVITATION_CODE_MAX_LENGTH,
        unique=True,
        validators=[invitation_code_validator],
        error_messages={"unique": "该邀请码已存在"},
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建者",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_invitation_codes",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    valid_days = models.PositiveSmallIntegerField(
        "有效天数",
        validators=[
            MinValueValidator(INVITATION_CODE_VALID_DAYS_MIN),
            MaxValueValidator(INVITATION_CODE_VALID_DAYS_MAX),
        ],
    )
    expires_at = models.DateTimeField("到期时间", editable=False)
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "邀请码"
        verbose_name_plural = "邀请码"
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                check=models.Q(
                    valid_days__gte=INVITATION_CODE_VALID_DAYS_MIN,
                    valid_days__lte=INVITATION_CODE_VALID_DAYS_MAX,
                ),
                name="users_invitation_days_range",
            ),
        ]

    def __str__(self):
        return self.code

    @property
    def is_valid(self) -> bool:
        """Active and not past its fixed expiry (UI-facing name)."""
        return bool(self.is_active and self.expires_at and self.expires_at > timezone.now())

    # Backwards-friendly alias used by the service layer/tests.
    is_usable = is_valid

    def _validate_immutable_window(self):
        if self._state.adding or not self.pk:
            return
        previous = (
            type(self)
            .objects.filter(pk=self.pk)
            .values("valid_days", "expires_at")
            .first()
        )
        if previous is None:
            return
        if self.valid_days != previous["valid_days"]:
            raise ValidationError({"valid_days": "邀请码创建后不能修改有效天数"})
        if self.expires_at is not None and previous["expires_at"] and self.expires_at > previous["expires_at"]:
            raise ValidationError({"expires_at": "邀请码到期时间只能提前，不能延长"})

    def clean(self):
        super().clean()
        self._validate_immutable_window()

    def save(self, *args, **kwargs):
        if self._state.adding:
            # Authoritative window: computed from the creation moment, never
            # from a caller-supplied value.
            self.expires_at = timezone.now() + timedelta(days=self.valid_days)
        else:
            self._validate_immutable_window()
        super().save(*args, **kwargs)


class RegistrationProvision(models.Model):
    """Durable, retryable record of the key created during registration.

    ``synced_at`` doubles as the queue marker: NULL means the key still has to
    be pushed to CPA. The row is deliberately kept after a successful sync so
    registration attribution (which invitation, which key) stays auditable.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.CASCADE,
        related_name="registration_provision",
    )
    invitation = models.ForeignKey(
        InvitationCode,
        verbose_name="邀请码",
        on_delete=models.PROTECT,
        related_name="registrations",
    )
    api_key = models.OneToOneField(
        "api_keys.APIKey",
        verbose_name="API 密钥",
        on_delete=models.CASCADE,
        related_name="registration_provision",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    synced_at = models.DateTimeField("同步完成时间", null=True, blank=True)
    last_attempt_at = models.DateTimeField("最近尝试时间", null=True, blank=True)
    attempts = models.PositiveIntegerField("尝试次数", default=0)

    class Meta:
        verbose_name = "注册密钥同步任务"
        verbose_name_plural = "注册密钥同步任务"
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["synced_at"], name="users_provision_synced_idx"),
        ]

    def __str__(self):
        return f"{self.user.username} <- {self.invitation.code}"
