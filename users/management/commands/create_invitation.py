"""Create one invitation code from the command line (deployment bootstrap).

Idempotent on purpose: re-running the deployment must never extend an existing
code's expiry, change its valid days, or re-enable a code an administrator has
stopped.  No invitation data is shipped in migrations; production creates the
first code with this command.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.utils import is_portal_admin
from users.models import INVITATION_CODE_VALID_DAYS_MAX, INVITATION_CODE_VALID_DAYS_MIN, InvitationCode
from users.registration import unique_invitation_code

User = get_user_model()


class Command(BaseCommand):
    help = "创建一个邀请码（已存在时不修改到期时间/有效天数/启停状态）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--code",
            default=None,
            help="邀请码；省略时随机生成 24 位大写字母数字",
        )
        parser.add_argument(
            "--days",
            type=int,
            required=True,
            help=f"有效期整数天，{INVITATION_CODE_VALID_DAYS_MIN}-{INVITATION_CODE_VALID_DAYS_MAX}",
        )
        parser.add_argument(
            "--admin",
            default="admin",
            help="创建者管理员用户名（默认 admin）",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if not INVITATION_CODE_VALID_DAYS_MIN <= days <= INVITATION_CODE_VALID_DAYS_MAX:
            raise CommandError(
                f"--days 必须是 {INVITATION_CODE_VALID_DAYS_MIN}-"
                f"{INVITATION_CODE_VALID_DAYS_MAX} 的整数"
            )

        admin_username = options["admin"]
        admin_user = User.objects.filter(username=admin_username).first()
        if admin_user is None:
            raise CommandError(f"管理员账号不存在: {admin_username}")
        if not is_portal_admin(admin_user):
            raise CommandError(f"账号 {admin_username} 不是管理员")

        code = (options.get("code") or "").strip() or unique_invitation_code()

        existing = InvitationCode.objects.filter(code=code).first()
        if existing is not None:
            # Deliberately untouched: an existing code keeps its original window
            # and state, so a repeated deployment cannot extend it.
            self.stdout.write(
                self.style.WARNING(
                    "邀请码已存在，未修改："
                    f"code={existing.code} days={existing.valid_days} "
                    f"expires_at={self._format(existing.expires_at)} "
                    f"active={'yes' if existing.is_active else 'no'}"
                )
            )
            return

        invitation = InvitationCode(code=code, valid_days=days, created_by=admin_user)
        try:
            # expires_at is computed authoritatively in save(); no need (and no
            # way) to accept a caller-supplied window.
            invitation.full_clean(exclude=["expires_at"])
        except ValidationError as exc:
            raise CommandError("; ".join(exc.messages)) from exc
        invitation.save()

        self.stdout.write(
            self.style.SUCCESS(
                "已创建邀请码："
                f"code={invitation.code} days={invitation.valid_days} "
                f"expires_at={self._format(invitation.expires_at)} "
                f"created_by={admin_user.username}"
            )
        )

    @staticmethod
    def _format(value):
        if value is None:
            return "-"
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%Y-%m-%d %H:%M:%S")
