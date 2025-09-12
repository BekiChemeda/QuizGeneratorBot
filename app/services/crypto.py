from __future__ import annotations

import base64
import hashlib
from typing import Optional
from cryptography.fernet import Fernet


def _derive_fernet_key(secret: str) -> bytes:
    # Derive a 32-byte base64 key from arbitrary secret
    digest = hashlib.sha256((secret or "change-me-secret").encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_text(plaintext: str, secret: str) -> str:
    key = _derive_fernet_key(secret)
    f = Fernet(key)
    token = f.encrypt((plaintext or "").encode("utf-8"))
    return token.decode("utf-8")


def decrypt_text(ciphertext: str, secret: str) -> Optional[str]:
    try:
        key = _derive_fernet_key(secret)
        f = Fernet(key)
        data = f.decrypt((ciphertext or "").encode("utf-8"))
        return data.decode("utf-8")
    except Exception:
        return None

