"""Minimal Telegram Bot API client (outgoing calls only; updates arrive via the webhook)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from app.services.retry import call_with_retries

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096


class TelegramApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.retryable = retryable


class TelegramClient(Protocol):
    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]: ...

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any] | None: ...

    def answer_callback_query(self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False) -> None: ...


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """[[(label, callback_data), ...], ...] -> InlineKeyboardMarkup"""
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]}


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (truncated)"


class TelegramBotApi:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        timeout: float = 15.0,
        max_attempts: int = 4,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = httpx.Client(base_url=f"{base_url.rstrip('/')}/bot{token}/", timeout=timeout, transport=transport)
        self._max_attempts = max_attempts
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        def attempt() -> Any:
            try:
                response = self._client.post(method, json=payload or {})
            except httpx.TransportError as exc:
                raise TelegramApiError(f"{method}: network error {type(exc).__name__}", retryable=True) from exc
            try:
                data = response.json()
            except ValueError:
                data = {}
            if response.status_code == 200 and data.get("ok"):
                return data.get("result")
            retry_after = (data.get("parameters") or {}).get("retry_after")
            description = data.get("description") or response.text[:200]
            raise TelegramApiError(
                f"{method}: {response.status_code} {description}",
                status=response.status_code,
                retry_after=float(retry_after) if retry_after is not None else None,
                retryable=response.status_code == 429 or response.status_code >= 500,
            )

        return call_with_retries(
            attempt,
            is_retryable=lambda exc: isinstance(exc, TelegramApiError) and exc.retryable,
            retry_after=lambda exc: getattr(exc, "retry_after", None),
            max_attempts=self._max_attempts,
            sleep=self._sleep,
            description=f"telegram.{method}",
        )

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": truncate(text),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        return self.call("sendMessage", payload)

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, reply_markup: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": truncate(text),
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": reply_markup or {"inline_keyboard": []},
        }
        try:
            return self.call("editMessageText", payload)
        except TelegramApiError as exc:
            if "message is not modified" in str(exc):
                return None
            raise

    def answer_callback_query(self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id, "show_alert": show_alert}
        if text:
            payload["text"] = text[:200]
        try:
            self.call("answerCallbackQuery", payload)
        except TelegramApiError as exc:
            # Callback queries expire after a while; failing to answer one is harmless.
            log.warning("answerCallbackQuery failed", extra={"error": str(exc)})

    # --- Webhook management (used by scripts/set_webhook.py) ------------------------------

    def set_webhook(self, url: str, *, secret_token: str, drop_pending_updates: bool = False, max_connections: int = 10) -> Any:
        return self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": drop_pending_updates,
                "max_connections": max_connections,
            },
        )

    def delete_webhook(self, *, drop_pending_updates: bool = False) -> Any:
        return self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    def get_webhook_info(self) -> Any:
        return self.call("getWebhookInfo")

    def get_me(self) -> Any:
        return self.call("getMe")
