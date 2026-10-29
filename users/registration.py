"""Invitation-code self-registration and its CPA key synchronisation.

Two entry points are meant to be used by views/commands:

``register_member(cleaned_data)``
    Create the ordinary member account, its pending key and the durable
    :class:`~users.models.RegistrationProvision` in one transaction.  The caller
    performs the first CPA push *after* the transaction commits by calling
    ``sync_registration_key(user.pk)``.

``sync_registration_key(user_id)``
    Idempotently push the registration key to CLIProxyAPI and only then mark the
    portal key active.  It returns ``True`` when the key is active and synced,
    ``False`` for a pending/retryable or ineligible task.

Business validation failures use :class:`django.core.exceptions.ValidationError`
so views can render them as ordinary form/business errors.
"""
import logging
import re
import secrets
import string
from functools import lru_cache

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from pypinyin import Style, lazy_pinyin, pinyin

from api_keys.cpa_sync import push_key_rotation_to_cliproxy
from api_keys.member_tokens import PINYIN_MAP
from api_keys.models import APIKey, APIKeyAlias
from users.models import InvitationCode, Profile, RegistrationProvision

logger = logging.getLogger(__name__)

User = get_user_model()

# --- registration key format -------------------------------------------------
# pioneer_sk_<16 random ASCII alphanumerics><PinyinUsername>
# Deliberately separate from api_keys.member_tokens.generate_member_key: the
# existing "sk-..." / rotation key format must not change.
REGISTRATION_KEY_PREFIX = "pioneer_sk_"
REGISTRATION_KEY_RANDOM_LENGTH = 16
_KEY_ALPHABET = string.ascii_letters + string.digits

# --- invitation codes --------------------------------------------------------
RANDOM_INVITATION_CODE_LENGTH = 24
_INVITATION_CODE_ALPHABET = string.ascii_uppercase + string.digits
_INVITATION_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USERNAME_RE = re.compile(r"^[A-Za-z]{2,64}$")
_HAN_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3007]")


def _han_characters(text: str) -> list[str]:
    return _HAN_RE.findall(text or "")


def generate_invitation_code(length: int = RANDOM_INVITATION_CODE_LENGTH) -> str:
    """Return a random uppercase-alphanumeric invitation code (not yet unique)."""
    if length < 1:
        raise ValueError("length must be positive")
    return "".join(secrets.choice(_INVITATION_CODE_ALPHABET) for _ in range(length))


def unique_invitation_code(length: int = RANDOM_INVITATION_CODE_LENGTH) -> str:
    for _ in range(10):
        code = generate_invitation_code(length)
        if not InvitationCode.objects.filter(code=code).exists():
            return code
    raise ValidationError("生成邀请码失败，请稍后重试")


@lru_cache(maxsize=1024)
def _readings_for_char(char: str) -> tuple[str, ...]:
    """All ASCII pinyin readings pypinyin knows for one Han character."""
    readings: list[str] = []
    for group in pinyin(char, style=Style.NORMAL, heteronym=True):
        for raw in group:
            reading = str(raw).strip().lower()
            if reading and re.fullmatch(r"[a-z]+", reading) and reading not in readings:
                readings.append(reading)
    return tuple(readings)


def _segment_legacy_readings(han: str, encoded: str) -> list[str] | None:
    """Pick one pypinyin reading per character so their join equals ``encoded``.

    ``PINYIN_MAP`` predates per-syllable capitalisation (e.g. ``BoHanbo``); using
    it only as a reading hint keeps the pronunciation the portal has always used
    while the caller emits ``BoHanBo``.  Returns ``None`` when no segmentation
    exists, in which case the caller falls back to pypinyin's default.
    """
    options = [_readings_for_char(char) for char in han]
    if any(not readings for readings in options):
        return None
    target = encoded.lower()

    @lru_cache(maxsize=None)
    def walk(index: int, position: int):
        if index == len(options):
            return () if position == len(target) else None
        for reading in options[index]:
            if target.startswith(reading, position):
                rest = walk(index + 1, position + len(reading))
                if rest is not None:
                    return (reading,) + rest
        return None

    result = walk(0, 0)
    return list(result) if result is not None else None


def username_from_display_name(display_name: str) -> str:
    """Canonical username: pinyin of every Han character, each syllable capitalised.

    ``薄汉博`` -> ``BoHanBo`` and ``黄沛玥`` -> ``HuangPeiYue``.  ``PINYIN_MAP``
    is consulted only to resolve readings for long-standing portal names, never
    for its mixed-case spelling.
    """
    han = "".join(_han_characters(display_name))
    if not han:
        raise ValueError("显示名必须包含中文姓名")

    syllables = None
    legacy = PINYIN_MAP.get(han)
    if legacy:
        syllables = _segment_legacy_readings(han, legacy)
    if syllables is None:
        syllables = []
        for char in han:
            parts = lazy_pinyin(char, style=Style.NORMAL)
            if len(parts) != 1 or not re.fullmatch(r"[a-z]+", parts[0]):
                raise ValueError("无法根据中文姓名生成拼音用户名，请联系管理员")
            syllables.append(parts[0])

    account_name = "".join(syllable.capitalize() for syllable in syllables)
    if not re.fullmatch(r"[A-Za-z]+", account_name):
        raise ValueError("无法根据中文姓名生成拼音用户名，请联系管理员")
    return account_name


def _can_segment(readings: list[tuple[str, ...]], username: str) -> bool:
    """Can ``username`` be split into one capitalised pinyin syllable per char?"""
    length = len(username)

    @lru_cache(maxsize=None)
    def walk(index: int, position: int) -> bool:
        if index == len(readings):
            return position == length
        for reading in readings[index]:
            token = reading.capitalize()
            if username.startswith(token, position) and walk(index + 1, position + len(token)):
                return True
        return False

    return walk(0, 0)


def username_matches_display_name(display_name: str, username: str) -> bool:
    """Whether ``username`` is a reasonable pinyin rendering of ``display_name``.

    The canonical rendering always matches.  Polyphonic characters may use any
    reading pypinyin knows (e.g. 单 -> ``Dan…`` or ``Shan…``), so users are not
    forced into one arbitrary "correct" pronunciation.  If pypinyin has no
    reading for one of the characters, fail closed: return ``False`` rather
    than accept an arbitrary username.  ``username_from_display_name`` normally
    rejects such names first, so this is a defensive guard for unusual
    pypinyin data.
    """
    try:
        canonical = username_from_display_name(display_name)
    except ValueError:
        return False
    if username == canonical:
        return True

    readings = []
    for char in _han_characters(display_name):
        candidates = _readings_for_char(char)
        if not candidates:
            return False
        readings.append(candidates)
    return bool(readings) and _can_segment(readings, username)


def generate_registration_key(username: str) -> str:
    """Return ``pioneer_sk_<16 alnum><username>`` (not yet checked for collisions)."""
    random_part = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(REGISTRATION_KEY_RANDOM_LENGTH))
    return f"{REGISTRATION_KEY_PREFIX}{random_part}{username}"


def unique_registration_key(username: str) -> str:
    """Generate a key whose hash is not used by an active key or a rotation alias."""
    for _ in range(5):
        raw_key = generate_registration_key(username)
        digest = APIKey.hash_token(raw_key)
        if APIKey.objects.filter(token_hash=digest).exists():
            continue
        if APIKeyAlias.objects.filter(token_hash=digest).exists():
            continue
        return raw_key
    raise ValidationError("生成注册密钥失败，请稍后重试")


def _clean_invitation_code(raw_code: str) -> str:
    code = (raw_code or "").strip()
    if not code:
        raise ValidationError("请输入邀请码")
    if not _INVITATION_CODE_RE.fullmatch(code):
        raise ValidationError("邀请码格式不正确")
    return code


def register_member(cleaned_data: dict):
    """Create an ordinary member account from validated registration data.

    Everything (user, profile, pending API key, provision task) is written in a
    single transaction; nothing is committed when the invitation is invalid, the
    username is taken, or the caller's transaction later rolls back.  The key is
    intentionally left ``pending`` until ``sync_registration_key`` succeeds.

    Only the five registration fields are read: submitted ``is_staff``,
    ``is_superuser`` or ``role`` values can never influence the created account.
    """
    display_name = str(cleaned_data.get("display_name") or "").strip()
    username = str(cleaned_data.get("username") or "").strip()
    invitation_code = _clean_invitation_code(str(cleaned_data.get("invitation_code") or ""))
    password = cleaned_data.get("password1") or cleaned_data.get("password") or ""

    if not display_name:
        raise ValidationError("请输入中文姓名")
    if len(display_name) > 50:
        raise ValidationError("中文姓名过长")
    if not username and display_name:
        try:
            username = username_from_display_name(display_name)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    if not _USERNAME_RE.fullmatch(username):
        raise ValidationError("用户名必须是 2-64 位 ASCII 拼音字母")
    if not password:
        raise ValidationError("请设置密码")

    with transaction.atomic():
        invitation = (
            InvitationCode.objects.select_for_update().filter(code=invitation_code).first()
        )
        if invitation is None:
            raise ValidationError("邀请码无效，请检查后重试")
        if not invitation.is_active:
            raise ValidationError("邀请码已停用")
        if not invitation.expires_at or invitation.expires_at <= timezone.now():
            raise ValidationError("邀请码已过期")

        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("该用户名已被使用，请更换后重试")

        # is_staff/is_superuser are always forced off: registration must never
        # be able to grant privileges, regardless of extra submitted data.
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username,
                    password=password,
                    is_staff=False,
                    is_superuser=False,
                    is_active=True,
                )
        except IntegrityError as exc:
            raise ValidationError("该用户名已被使用，请更换后重试") from exc

        profile, _ = Profile.objects.get_or_create(
            user=user,
            defaults={"display_name": display_name, "role": Profile.ROLE_USER},
        )
        profile.display_name = display_name
        profile.role = Profile.ROLE_USER
        profile.save(update_fields=["display_name", "role", "updated_at"])
        # The post_save signal seeded the reverse cache with a profile whose
        # display_name was the username; drop it so callers see the real row.
        user._state.fields_cache.pop("profile", None)

        raw_key = unique_registration_key(username)
        metadata = APIKey.build_token_metadata(raw_key)
        api_key = APIKey.objects.create(
            user=user,
            name="API 密钥",
            token_hash=metadata["token_hash"],
            token_prefix=metadata["token_prefix"],
            token_masked=metadata["token_masked"],
            token_plaintext=raw_key,
            source=APIKey.SOURCE_PORTAL,
            status=APIKey.STATUS_PENDING,
            note="邀请码注册自动生成",
        )
        RegistrationProvision.objects.create(
            user=user,
            invitation=invitation,
            api_key=api_key,
        )

    logger.info("Registration provision created for user %s", user.pk)
    return user


def sync_registration_key(user_id: int) -> bool:
    """Push one user's registration key to CPA, then activate it in the portal.

    The provision row is locked so concurrent callers cannot both push/activate.
    The CPA call uses ``PATCH {"old": key, "new": key}``: when the key is already
    present it is replaced by itself, when absent it is appended once, so running
    the same sync repeatedly is safe.  A transport/HTTP failure leaves the key
    ``pending`` with ``synced_at`` NULL so the periodic command retries it.

    Returns ``True`` only when the key is active and ``synced_at`` is recorded.
    """
    with transaction.atomic():
        provision = (
            RegistrationProvision.objects.select_for_update()
            .select_related("user", "api_key")
            .filter(user_id=user_id)
            .first()
        )
        if provision is None:
            logger.warning("Registration key sync skipped: no provision for user %s", user_id)
            return False

        user = provision.user
        api_key = provision.api_key
        if not user.is_active:
            # Disabled (or otherwise inactive) accounts must never be activated
            # by the retry loop; leave the task pending for an administrator.
            logger.warning("Registration key sync skipped: user %s is inactive", user_id)
            return False
        if provision.synced_at is not None:
            return api_key.status == APIKey.STATUS_ACTIVE
        if api_key.status not in (APIKey.STATUS_PENDING, APIKey.STATUS_ACTIVE):
            # Revoked/expired keys are administrator decisions: never revive them.
            logger.warning(
                "Registration key sync skipped: key for user %s has status %s",
                user_id,
                api_key.status,
            )
            return False
        if not api_key.token_plaintext:
            logger.error("Registration key sync failed: key for user %s has no plaintext", user_id)
            return False

        provision.attempts += 1
        provision.last_attempt_at = timezone.now()
        provision.save(update_fields=["attempts", "last_attempt_at"])

        try:
            push_key_rotation_to_cliproxy(api_key.token_plaintext, api_key.token_plaintext)
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP failure is retryable
            # Log only the exception type: exception text/response bodies must
            # never leak the member's complete key.
            logger.warning(
                "Registration key CPA sync failed for user %s (attempt %s): %s",
                user_id,
                provision.attempts,
                type(exc).__name__,
            )
            return False

        if api_key.status != APIKey.STATUS_ACTIVE:
            api_key.status = APIKey.STATUS_ACTIVE
            api_key.save(update_fields=["status"])
        provision.synced_at = timezone.now()
        provision.save(update_fields=["synced_at"])

    logger.info("Registration key synced and activated for user %s", user_id)
    return True
