from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.settings import get_settings


def encrypt_email_password(password: str) -> str:
    key = _credential_key()
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, password.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def decrypt_email_password(ciphertext: str) -> str:
    payload = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    if len(payload) < 29:
        raise ValueError("Invalid encrypted email credential")
    return AESGCM(_credential_key()).decrypt(payload[:12], payload[12:], None).decode("utf-8")


def _credential_key() -> bytes:
    value = get_settings().email_credential_key.strip()
    if not value:
        raise RuntimeError("EMAIL_CREDENTIAL_KEY is not configured")
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("EMAIL_CREDENTIAL_KEY must be URL-safe Base64") from exc
    if len(key) != 32:
        raise RuntimeError("EMAIL_CREDENTIAL_KEY must decode to exactly 32 bytes")
    return key
