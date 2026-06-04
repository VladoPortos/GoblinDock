"""Password hashing, at-rest encryption, password policy, CSRF + token helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, Exception):  # noqa: BLE001
        return False


def password_problem(password: str) -> str | None:
    """Return a human message if the password is too weak, else None."""
    if len(password or "") < 10:
        return "Password must be at least 10 characters."
    classes = sum(bool(set(password) & s) for s in (
        set("abcdefghijklmnopqrstuvwxyz"),
        set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
        set("0123456789"),
        set("!@#$%^&*()-_=+[]{};:,.<>/?`~|\\\"' "),
    ))
    if classes < 3:
        return "Use at least 3 of: lowercase, uppercase, digits, symbols."
    return None


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    # Derive a dedicated 32-byte encryption key from the secret key via HKDF
    # (separate domain from session signing; app-specific salt + info).
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=b"goblindock.enc.v1", info=b"secrets-at-rest")
    key = hkdf.derive(settings.secret_key.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    if value is None:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):  # noqa: BLE001
        return ""  # rotated/corrupt — fail closed


def mask(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return value[:keep] + "•" * max(4, len(value) - keep)


def new_csrf_token() -> str:
    return _secrets.token_urlsafe(32)


def csrf_ok(sent: str | None, expected: str | None) -> bool:
    if not sent or not expected:
        return False
    return hmac.compare_digest(sent, expected)


# --------------------------------------------------------------------------- #
# Widget API keys — a per-user, read-only token for the Homepage widget.       #
# Prefix-tagged + high-entropy; only its sha256 hash is ever stored, and the   #
# plaintext is shown to the user exactly once at generation.                   #
# --------------------------------------------------------------------------- #
WIDGET_KEY_PREFIX = "gdwk_"


def new_widget_key() -> str:
    """Mint a fresh widget key: the ``gdwk_`` tag + a 256-bit URL-safe token."""
    return WIDGET_KEY_PREFIX + _secrets.token_urlsafe(32)


@lru_cache(maxsize=1)
def _widget_key_mac_key() -> bytes:
    # Dedicated HMAC key derived from the app secret (HKDF — separate `info` domain
    # from the Fernet key above). Keying the digest means a DB leak alone, without
    # the secret key, can't even offline-verify a guessed token.
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32,
                salt=b"goblindock.enc.v1", info=b"widget-key-mac")
    return hkdf.derive(settings.secret_key.encode("utf-8"))


def hash_widget_key(token: str) -> str:
    """Keyed HMAC-SHA256 (hex) of a widget key.

    The token is already a 256-bit random secret, so a fast keyed MAC — not a slow
    password KDF — is the right verification primitive (a KDF would only add latency
    to every poll for no real gain). HMAC keeps the stored digest from being
    offline-verifiable without the app secret."""
    return hmac.new(_widget_key_mac_key(), (token or "").encode("utf-8"),
                    hashlib.sha256).hexdigest()
