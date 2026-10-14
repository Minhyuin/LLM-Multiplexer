"""Self-service rotation for a member's own active API key."""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from api_keys.cpa_sync import push_key_rotation_to_cliproxy
from api_keys.member_tokens import MEMBER_KEY_RANDOM_LENGTH, build_account_name, generate_member_key
from api_keys.models import APIKey, APIKeyAlias
from core.utils import is_portal_admin

logger = logging.getLogger(__name__)

ROTATE_COOLDOWN = timedelta(minutes=10)


class RotationError(Exception):
    """A rotation that must not change the stored key. Message is safe to show."""


def cooldown_active(user) -> bool:
    latest = (
        APIKey.objects.filter(user=user, token_rotated_at__isnull=False)
        .order_by("-token_rotated_at")
        .values_list("token_rotated_at", flat=True)
        .first()
    )
    return bool(latest and timezone.now() - latest < ROTATE_COOLDOWN)


def self_rotate_state(user) -> dict:
    return {
        "can_self_rotate": user.is_authenticated and not is_portal_admin(user),
        "rotate_blocked": user.is_authenticated and cooldown_active(user),
    }


def account_name_for_user(user) -> str:
    profile = getattr(user, "profile", None)
    raw = (profile.display_name if profile and profile.display_name else "") or user.username
    try:
        return build_account_name(raw)
    except ValueError as exc:
        raise RotationError("无法根据姓名生成密钥，请联系管理员") from exc


def unique_member_key(account_name: str) -> str:
    for _ in range(5):
        candidate = generate_member_key(account_name, MEMBER_KEY_RANDOM_LENGTH)
        digest = APIKey.hash_token(candidate)
        if APIKey.objects.filter(token_hash=digest).exists():
            continue
        if APIKeyAlias.objects.filter(token_hash=digest).exists():
            continue
        return candidate
    raise RotationError("无法生成不重复的密钥，请重试")


def _write_rotation(key, old_plaintext: str, new_plaintext: str, when) -> None:
    metadata = APIKey.build_token_metadata(new_plaintext)
    APIKeyAlias.objects.get_or_create(
        token_hash=key.token_hash,
        defaults={
            "user": key.user,
            "token_prefix": key.token_prefix,
            "token_masked": key.token_masked,
            "token_plaintext": old_plaintext,
            "note": "成员自助轮换，轮换于 " + when.strftime("%Y-%m-%d %H:%M"),
        },
    )
    key.token_hash = metadata["token_hash"]
    key.token_prefix = metadata["token_prefix"]
    key.token_masked = metadata["token_masked"]
    key.token_plaintext = new_plaintext
    key.token_rotated_at = when
    key.save(
        update_fields=[
            "token_hash",
            "token_prefix",
            "token_masked",
            "token_plaintext",
            "token_rotated_at",
        ]
    )


def rotate_member_key(user, key_id: int) -> str:
    """Replace the member's own active key. CPA is updated before the portal row.

    Returns the new plaintext. Raises RotationError without leaving a new portal
    key when the change cannot be completed. If the portal write fails after CPA
    accepted the new key, CPA is patched back to the old key.
    """
    if is_portal_admin(user):
        raise RotationError("管理员请在用户编辑页更换密钥")

    new_token = ""
    old_token = ""
    with transaction.atomic():
        owned = list(APIKey.objects.select_for_update().filter(user=user).order_by("pk"))
        key = next((item for item in owned if item.pk == int(key_id)), None)
        if key is None:
            raise APIKey.DoesNotExist
        if key.status != APIKey.STATUS_ACTIVE:
            raise RotationError("只能更换启用中的密钥")
        old_token = key.token_plaintext or ""
        if not old_token:
            raise RotationError("当前密钥没有可同步的明文，请联系管理员")
        if cooldown_active(user):
            raise RotationError("10 分钟内只能更换一次密钥")

        account_name = account_name_for_user(user)
        new_token = unique_member_key(account_name)
        try:
            push_key_rotation_to_cliproxy(old_token, new_token)
        except Exception as exc:
            logger.error("Member key CPA sync failed for user %s: %s", user.pk, exc)
            raise RotationError("同步代理失败，密钥未更换") from exc
        try:
            _write_rotation(key, old_token, new_token, timezone.now())
        except Exception as exc:
            logger.exception("Member key portal save failed for user %s", user.pk)
            try:
                push_key_rotation_to_cliproxy(new_token, old_token)
            except Exception:
                logger.exception("Member key CPA rollback failed for user %s", user.pk)
                raise RotationError("门户保存失败，且代理回滚失败，请立即联系管理员") from exc
            raise RotationError("门户保存失败，已恢复旧密钥") from exc
    return new_token
