"""Routes one Telegram update to the right handler (runs in the background worker)."""

from __future__ import annotations

from app.bot.card_handler import handle_card_message
from app.bot.commands import handle_command
from app.bot.confirmation_handler import handle_callback
from app.bot.message_handler import handle_report_message
from app.container import AppContainer
from app.database.models import UpdateStatus
from app.schemas.telegram import TgUpdate
from app.services.card_parser import is_card_message


def dispatch_update(container: AppContainer, update: TgUpdate) -> UpdateStatus:
    if update.callback_query is not None:
        handle_callback(container, update.callback_query)
        return UpdateStatus.PROCESSED

    message = update.message
    if message is None:
        return UpdateStatus.IGNORED  # edited messages, channel posts, membership changes…
    if message.from_user is None or message.from_user.is_bot:
        return UpdateStatus.IGNORED

    content = message.content
    if content and content.lstrip().startswith("/"):
        handle_command(container, update.update_id, message)
        return UpdateStatus.PROCESSED
    if content and is_card_message(content):
        return handle_card_message(container, update.update_id, message)
    if content and content.strip():
        return handle_report_message(container, update.update_id, message)

    if message.chat.is_private:
        container.telegram.send_message(
            message.chat.id,
            "This message type is not supported. Please forward the WhatsApp loader report as text.",
            reply_to_message_id=message.message_id,
        )
    return UpdateStatus.IGNORED
