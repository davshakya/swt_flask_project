"""Authenticated encryption for raw device keys needed by OTA signing."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet(app_secret):
    secret = str(app_secret or "").encode("utf-8")
    if not secret:
        raise ValueError("APP_SECRET_KEY is required for the device key vault")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def encrypt_device_key(device_key, app_secret):
    raw_key = str(device_key or "").strip()
    if not raw_key:
        return ""
    return _fernet(app_secret).encrypt(raw_key.encode("utf-8")).decode("ascii")


def decrypt_device_key(ciphertext, app_secret):
    token = str(ciphertext or "").strip()
    if not token:
        return ""
    try:
        return _fernet(app_secret).decrypt(token.encode("ascii")).decode("utf-8").strip()
    except (InvalidToken, UnicodeError, ValueError):
        return ""
