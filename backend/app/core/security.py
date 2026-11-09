"""Security helpers: webhook secret verification and secret generation."""

from __future__ import annotations

import hmac
import secrets

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def verify_webhook_secret(provided: str | None, expected: str) -> bool:
    """Constant-time comparison of the secret Telegram sends with every webhook request."""
    if not expected or provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def generate_webhook_secret(length: int = 48) -> str:
    # token_urlsafe only uses A-Z a-z 0-9 - _, which is exactly what Telegram allows.
    return secrets.token_urlsafe(length)[:256]


def redact(value: str, keep: int = 4) -> str:
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "…" + "*" * 4
