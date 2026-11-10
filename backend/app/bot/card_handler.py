"""Typed trip-card / HSD messages: parse -> preview against the card block -> Confirm -> write."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from app.bot.formatting import fmt_date, h, loader_label
from app.container import AppContainer
from app.database.models import CardEntry, CardStatus, UpdateStatus, User, utcnow
from app.database.repositories import CardRepository, audit
from app.schemas.report import SHIFT_LABELS
from app.schemas.telegram import TgCallbackQuery, TgMessage
from app.services.card_parser import CARD_FORMAT, CardParseError, parse_card
from app.services.card_service import fmt_dec
from app.services.telegram_service import TelegramApiError, inline_keyboard
from app.worker.job_queue import JobType, enqueue

log = logging.getLogger(__name__)

CARD_FORMAT_HELP = (
    "<b>Trip card / HSD message</b>\n"
    f"<code>{h(CARD_FORMAT)}</code>\n\n"
    "• Send only what you have: one shift, two shifts, all three, or only HSD. Each line is saved "
    "in its own cell; the other cells are left as they are.\n"
    "• One line works too: <code>CARD L11 06-11 3rd 44 HSD 154.98</code>\n"
    "• HSD: each tanker fill in litres. A fill already saved for that day is not added twice, so "
    "you can send the full day again (<code>HSD: 190 + 154.98</code>). Two fills of exactly the "
    "same litres? Send them together: <code>HSD: 150 + 150</code>.\n"
    "• Trips for a shift that is already filled are replaced (shown before you confirm).\n"
    "• To fix a wrong HSD fill, edit the cell in the sheet."
)


def card_keyboard(card_id: int) -> dict:
    return inline_keyboard([[("✅ Confirm Update", f"kc:{card_id}"), ("✖️ Cancel", f"kx:{card_id}")]])


# --- Rendering ------------------------------------------------------------------------------------


def _litres(fills: list[str] | None) -> str:
    if fills is None:
        return "? (formula)"
    if not fills:
        return "–"
    total = fmt_dec(sum((Decimal(f) for f in fills), Decimal(0)))
    return f"{' + '.join(fills)} = {total} L" if len(fills) > 1 else f"{total} L"


def _chp(card_trips: int, report_trips: dict | None, shift: int) -> str:
    chp = (report_trips or {}).get(str(shift))
    if chp is None:
        return ""
    if chp == card_trips:
        return f" · CHP report {chp} ✅"
    return f" · CHP report {chp} (card {card_trips - chp:+d})"


def _lines(card: CardEntry) -> list[str]:
    preview = card.preview or {}
    cells = preview.get("cells") or {}
    lines = []
    for ref, cell in cells.items():
        if cell["kind"] == "trips":
            shift, after, before = cell["shift"], cell["after"], cell["before"]
            if before == after:
                state = "already in sheet"
            elif before is None:
                state = "new"
            else:
                state = f"⚠️ replaces {before}"
            lines.append(f"{SHIFT_LABELS[shift]}: {after} trips ({ref}, {state}){_chp(after, preview.get('report_trips'), shift)}")
    for ref, cell in cells.items():
        if cell["kind"] == "hsd":
            parts = []
            if cell["added"]:
                parts.append("adding " + " + ".join(cell["added"]))
            if cell["already"]:
                parts.append(" + ".join(cell["already"]) + " already saved")
            result = _litres(cell["existing"] + cell["added"])
            lines.append(f"HSD ({ref}): {', '.join(parts)} → {result}")
    return lines


_AFTER_CONFIRM = (CardStatus.QUEUED, CardStatus.APPLYING, CardStatus.UPDATED)


def render_card_summary(card: CardEntry, *, ttl_minutes: int, footer: str | None = None) -> str:
    head = f"🗂 <b>Trip card received</b> (#{card.id})\n{loader_label(card.loader_number)} · {fmt_date(card.entry_date)}"
    preview = card.preview or {}
    if card.worksheet_name and card.sheet_row:
        table = f"'{h(preview['table'])}' in " if preview.get("table") else ""
        head += f" → {table}'{h(card.worksheet_name)}' · row {card.sheet_row}"
    out = [head, ""]
    out += [h(line) for line in _lines(card)]
    day = preview.get("day")
    if day and card.status in (CardStatus.PENDING_CONFIRMATION, *_AFTER_CONFIRM):
        shifts = " · ".join(f"{label} {'–' if day.get(str(n)) is None else day[str(n)]}" for n, label in ((1, "1st"), (2, "2nd"), (3, "3rd")))
        hsd = f" · HSD {_litres(day.get('hsd'))}" if "hsd" in day else ""
        out += ["", f"Day after saving: {h(shifts + hsd)}"]
    out.append("")
    if footer:
        out.append(footer)
    elif card.status == CardStatus.PENDING_CONFIRMATION:
        out.append(f"Press Confirm Update to save, or Cancel. (Expires in {ttl_minutes} min.)")
    elif card.status == CardStatus.UNCHANGED:
        out.append("➖ Nothing to change — the sheet already has these values.")
    else:
        out.append(f"⛔ Not saved: {h(card.error_message or card.status.value)}")
    return "\n".join(out)


def render_card_result(card: CardEntry) -> str:
    where = f"{loader_label(card.loader_number)} · {fmt_date(card.entry_date)} · '{h(card.worksheet_name)}' row {card.sheet_row}"
    if card.status == CardStatus.UPDATED:
        lines = [f"✅ <b>Saved</b> trip card #{card.id}", where]
        for ref, cell in ((card.preview or {}).get("cells") or {}).items():
            if cell["before"] == cell["after"]:
                continue
            if cell["kind"] == "trips":
                lines.append(f"• {SHIFT_LABELS[cell['shift']]}: {cell['after']} trips ({ref})")
            else:
                lines.append(f"• HSD: {h(_litres(cell['existing'] + cell['added']))} ({ref})")
        return "\n".join(lines)
    icon = "⚠️" if card.status == CardStatus.CONFLICT else "❌"
    return f"{icon} <b>Trip card #{card.id} not saved</b>\n{where}\n{h(card.error_message or card.status.value)}"


# --- Message ------------------------------------------------------------------------------------


def handle_card_message(container: AppContainer, update_id: int, message: TgMessage) -> UpdateStatus:
    db, auth, telegram = container.db, container.auth, container.telegram
    chat = message.chat
    text = message.content or ""

    with db.session() as session:
        if not auth.chat_accepts_reports(session, chat):
            return UpdateStatus.IGNORED
        user = auth.resolve_user(session, message.from_user)
        existing = CardRepository(session).get_by_update(update_id)
        if user is None:
            audit(session, "card_rejected_unauthorized", actor=message.from_user.id if message.from_user else None, entity_type="chat", entity_id=chat.id)

    if user is None:
        if chat.is_private and message.from_user:
            telegram.send_message(
                chat.id,
                "⛔ You are not authorised to submit reports.\n"
                f"Your Telegram user ID is <code>{message.from_user.id}</code> — ask an administrator to run "
                f"<code>/grant_loader {message.from_user.id} &lt;loader&gt;</code>.",
                reply_to_message_id=message.message_id,
            )
        return UpdateStatus.IGNORED

    if existing is not None:
        if existing.summary_message_id is not None:
            return UpdateStatus.PROCESSED  # re-delivery after the summary was already sent
        card_id = existing.id
    else:
        try:
            parsed = parse_card(text, today=container.today())
        except CardParseError as exc:
            errors = "\n".join(f"• {h(e)}" for e in exc.errors)
            telegram.send_message(
                chat.id,
                f"⚠️ Could not read this trip card:\n{errors}\n\n{CARD_FORMAT_HELP}",
                reply_to_message_id=message.message_id,
            )
            return UpdateStatus.PROCESSED
        with db.session() as session:
            card = CardEntry(
                loader_number=parsed.loader_number,
                entry_date=parsed.entry_date,
                trips={str(k): v for k, v in parsed.trips.items()},
                hsd_fills=[str(f) for f in parsed.hsd_fills],
                telegram_user_id=user.telegram_user_id,
                chat_id=chat.id,
                source_message_id=message.message_id,
                source_update_id=update_id,
                raw_text=text,
            )
            session.add(card)
            session.flush()
            card_id = card.id
            audit(
                session,
                "card_received",
                actor=user.telegram_user_id,
                entity_type="card_entry",
                entity_id=card_id,
                loader_number=parsed.loader_number,
                date=parsed.entry_date,
                trips=card.trips,
                hsd=card.hsd_fills,
            )

    container.cards.preview(card_id)
    with db.session() as session:
        card = CardRepository(session).get(card_id)
        text_out = render_card_summary(card, ttl_minutes=container.settings.confirmation_ttl_minutes)
        pending = card.status == CardStatus.PENDING_CONFIRMATION
    sent = telegram.send_message(
        chat.id, text_out, reply_markup=card_keyboard(card_id) if pending else None, reply_to_message_id=message.message_id
    )
    with db.session() as session:
        CardRepository(session).get(card_id).summary_message_id = sent.get("message_id") if isinstance(sent, dict) else None
    return UpdateStatus.PROCESSED


# --- Buttons ------------------------------------------------------------------------------------


def _edit_summary(container: AppContainer, query: TgCallbackQuery, card_id: int, footer: str) -> None:
    if not query.message:
        return
    with container.db.session() as session:
        card = CardRepository(session).get(card_id)
        text = render_card_summary(card, ttl_minutes=container.settings.confirmation_ttl_minutes, footer=footer)
    try:
        container.telegram.edit_message_text(query.message.chat.id, query.message.message_id, text)
    except TelegramApiError as exc:
        log.warning("could not edit card summary", extra={"card_id": card_id, "error": str(exc)})


def confirm_card(container: AppContainer, query: TgCallbackQuery, user: User, card_id: int) -> None:
    telegram = container.telegram
    ttl = timedelta(minutes=container.settings.confirmation_ttl_minutes)
    with container.db.session() as session:
        repo = CardRepository(session)
        card = repo.get(card_id)
        if card is None:
            telegram.answer_callback_query(query.id, "Trip card not found.")
            return
        if card.telegram_user_id != user.telegram_user_id and not container.auth.is_admin(user):
            telegram.answer_callback_query(query.id, "Only the person who sent this (or an admin) can confirm it.", show_alert=True)
            return
        if card.status != CardStatus.PENDING_CONFIRMATION:
            telegram.answer_callback_query(query.id, f"Already handled ({card.status.value.lower()}).")
            return
        if utcnow() - card.created_at > ttl:
            repo.transition(card_id, [CardStatus.PENDING_CONFIRMATION], CardStatus.EXPIRED, completed_at=utcnow())
            expired = True
        elif not repo.transition(
            card_id, [CardStatus.PENDING_CONFIRMATION], CardStatus.QUEUED, confirmed_by=user.telegram_user_id, confirmed_at=utcnow()
        ):
            telegram.answer_callback_query(query.id, "Already handled.")
            return
        else:
            expired = False
            enqueue(session, JobType.APPLY_CARD, {"card_id": card_id}, max_attempts=container.settings.job_max_attempts)
            audit(session, "card_confirmed", actor=user.telegram_user_id, entity_type="card_entry", entity_id=card_id)

    if expired:
        _edit_summary(container, query, card_id, "⌛ This confirmation expired — please send the message again.")
        telegram.answer_callback_query(query.id, "Expired.")
        return
    _edit_summary(container, query, card_id, f"✅ Confirmed by {h(user.display_name)} — saving…")
    telegram.answer_callback_query(query.id, "Saving…")
    container.wake_worker()


def cancel_card(container: AppContainer, query: TgCallbackQuery, user: User, card_id: int) -> None:
    telegram = container.telegram
    with container.db.session() as session:
        repo = CardRepository(session)
        card = repo.get(card_id)
        if card is None:
            telegram.answer_callback_query(query.id, "Trip card not found.")
            return
        if card.telegram_user_id != user.telegram_user_id and not container.auth.is_admin(user):
            telegram.answer_callback_query(query.id, "Only the sender or an admin can cancel this.", show_alert=True)
            return
        if not repo.transition(card_id, [CardStatus.PENDING_CONFIRMATION], CardStatus.CANCELLED, completed_at=utcnow()):
            telegram.answer_callback_query(query.id, f"Already handled ({card.status.value.lower()}).")
            return
        audit(session, "card_cancelled", actor=user.telegram_user_id, entity_type="card_entry", entity_id=card_id)
    _edit_summary(container, query, card_id, f"✖️ Cancelled by {h(user.display_name)}. Nothing was saved.")
    telegram.answer_callback_query(query.id, "Cancelled.")


def send_card_result(container: AppContainer, card_id: int) -> None:
    with container.db.session() as session:
        card = CardRepository(session).get(card_id)
        if card is None or card.result_message_id is not None or card.status in (CardStatus.QUEUED, CardStatus.APPLYING):
            return
        text, chat_id, reply_to = render_card_result(card), card.chat_id, card.summary_message_id
    sent = container.telegram.send_message(chat_id, text, reply_to_message_id=reply_to)
    with container.db.session() as session:
        CardRepository(session).get(card_id).result_message_id = sent.get("message_id") if isinstance(sent, dict) else 0
