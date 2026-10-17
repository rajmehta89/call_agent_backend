"""Encrypt secrets (API tokens, client secrets) before they are stored in MongoDB.

The key is derived from AUTH_SECRET, so a database copy alone does not reveal the secrets.
Values saved before encryption existed are still read as plain text and get encrypted on
their next save. Changing AUTH_SECRET makes stored secrets unreadable: re-enter them.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)
PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    secret = os.getenv("AUTH_SECRET", "").strip()
    if not secret:
        raise RuntimeError("AUTH_SECRET must be set to store secrets")
    key = base64.urlsafe_b64encode(hashlib.sha256(f"secret-store:{secret}".encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value or ""  # legacy plain-text value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        logger.error("a stored secret could not be decrypted (AUTH_SECRET changed?); re-enter it in the dashboard")
        return ""
