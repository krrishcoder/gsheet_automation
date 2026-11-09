"""The subset of the Telegram Bot API update schema the bot relies on (unknown fields ignored)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _TgModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TgUser(_TgModel):
    id: int
    is_bot: bool = False
    first_name: str = ""
    last_name: str | None = None
    username: str | None = None

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()


class TgChat(_TgModel):
    id: int
    type: str  # private | group | supergroup | channel
    title: str | None = None
    username: str | None = None

    @property
    def is_private(self) -> bool:
        return self.type == "private"

    @property
    def is_group(self) -> bool:
        return self.type in {"group", "supergroup"}


class TgMessage(_TgModel):
    message_id: int
    date: int = 0
    chat: TgChat
    from_user: TgUser | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    forward_origin: dict[str, Any] | None = None
    forward_date: int | None = None  # pre-Bot-API-7 forward marker
    photo: list[dict[str, Any]] | None = None
    document: dict[str, Any] | None = None

    @property
    def content(self) -> str | None:
        """Message text, or the caption of a media message."""
        return self.text if self.text is not None else self.caption

    @property
    def is_forwarded(self) -> bool:
        return self.forward_origin is not None or self.forward_date is not None


class TgCallbackQuery(_TgModel):
    id: str
    from_user: TgUser = Field(alias="from")
    message: TgMessage | None = None
    data: str | None = None


class TgUpdate(_TgModel):
    update_id: int
    message: TgMessage | None = None
    edited_message: TgMessage | None = None
    channel_post: TgMessage | None = None
    callback_query: TgCallbackQuery | None = None

    @property
    def kind(self) -> str:
        for name in ("message", "callback_query", "edited_message", "channel_post"):
            if getattr(self, name) is not None:
                return name
        return "other"

    @property
    def actor(self) -> TgUser | None:
        if self.callback_query:
            return self.callback_query.from_user
        msg = self.message or self.edited_message
        return msg.from_user if msg else None

    @property
    def chat(self) -> TgChat | None:
        if self.callback_query and self.callback_query.message:
            return self.callback_query.message.chat
        msg = self.message or self.edited_message or self.channel_post
        return msg.chat if msg else None
