"""Member key format shared by import and self-service rotation.

Production keys look like ``sk-`` + random alphanumerics + the member's pinyin.
The pinyin suffix is recognizable and not secret; the random prefix is.
"""
import re
import secrets

from pypinyin import Style, lazy_pinyin


PINYIN_MAP = {
    "黄海": "HuangHai",
    "薄汉博": "BoHanbo",
    "黄沛玥": "HuangPeiyue",
}

_RANDOM_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
MEMBER_KEY_RANDOM_LENGTH = 16
IMPORT_KEY_RANDOM_LENGTH = 7


def build_account_name(name: str) -> str:
    if name in PINYIN_MAP:
        return PINYIN_MAP[name]
    sanitized = re.sub(r"\s+", "", str(name or "").strip())
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", sanitized):
        return sanitized
    pinyin_parts = lazy_pinyin(sanitized, style=Style.NORMAL)
    if not pinyin_parts:
        raise ValueError(f"姓名 {name} 无法转换为拼音")
    return "".join(part.capitalize() for part in pinyin_parts)


def generate_member_key(account_name: str, random_length: int = MEMBER_KEY_RANDOM_LENGTH) -> str:
    if random_length < 1:
        raise ValueError("random_length must be positive")
    prefix = "".join(secrets.choice(_RANDOM_ALPHABET) for _ in range(random_length))
    return f"sk-{prefix}{account_name}"
