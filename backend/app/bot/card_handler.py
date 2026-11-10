"""Card messages (trips, hour meter, diesel refills): parse -> preview -> Confirm -> write."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from app.bot.formatting import fmt_date, h, loader_label
from app.container import AppContainer
from app.database.models import CardEntry, CardStatus, UpdateStatus, User, utcnow
from app.database.repositories import CardRepository, audit
from app.schemas.telegram import TgCallbackQuery, TgMessage
from app.services.card_parser import CARD_FORMAT, CardParseError, parse_card
from app.services.card_service import fmt_dec
from app.services.telegram_service import TelegramApiError, inline_keyboard
from app.worker.job_queue import JobType, enqueue

log = logging.getLogger(__name__)

CARD_FORMAT_HELP = (
    "<b>Card message</b> (trips, hour meter, diesel)\n"
    f"<code>{h(CARD_FORMAT)}</code>\n\n"
    "<code>L11 10-11</code> first line: loader and date\n"
    "<code>T1</code> <code>T2</code> <code>T3</code> trips of the 1st / 2nd / 3rd shift → Card Details\n"
    "<code>O1</code> … <code>O3</code> opening HM, <code>C1</code> … <code>C3</code> closing HM → Opening and Closing\n"
    "<code>H 132.4 4276.2</code> diesel refill: litres, then the HM at the refill → hsd avg (new row, date "
    "filled in) and HSD Fuel in Card Details. One <code>H</code> per refill.\n\n"
    "• Send only what you have, e.g. <code>L11 10-11 C3 4291.5</code>; other cells are left as they are.\n"
    "• A value already in the sheet is replaced only after you see it in the preview and confirm.\n"
    "• A refill sent again (same litres and HM) is not added twice.\n"
    "• Refused: closing HM below opening, a refill HM lower than the last refill's. "
    "Warned: a shift over 8 h, an opening HM that differs from the previous closing."
)


def card_keyboard(card_id: int) -> dict:
    return inline_keyboard([[("✅ Confirm Update", f"kc:{card_id}"), ("✖️ Cancel", f"kx:{card_id}")]])


# --- Rendering ------------------------------------------------------------------------------------

_SHIFT = {1: "1st", 2: "2nd", 3: "3rd"}
_AFTER_CONFIRM = (CardStatus.QUEUED, CardStatus.APPLYING, CardStatus.UPDATED)


def _litres(fills: list[str] | None) -> str:
    if fills is None:
        return "? (formula)"
    if not fills:
        return "–"
    total = fmt_dec(sum((Decimal(f) for f in fills), Decimal(0)))
    return f"{' + '.join(fills)} = {total} L" if len(fills) > 1 else f"{total} L"


def _state(cell: dict) -> str:
    if cell["before"] == cell["after"]:
        return "already in sheet"
    return "new" if cell["before"] is None else f"⚠️ replaces {cell['before']}"


def _chp(card_trips: int, report_trips: dict | None, shift: int) -> str:
    chp = (report_trips or {}).get(str(shift))
    if chp is None:
        return ""
    if chp == card_trips:
        return f" · CHP report {chp} ✅"
    return f" · CHP report {chp} (card {card_trips - chp:+d})"


def _sections(card: CardEntry) -> list[str]:
    """Preview lines per block, in the order of the sheet (plain text; escaped by the caller)."""
    preview = card.preview or {}
    cells = preview.get("cells") or {}
    blocks, rows = preview.get("blocks") or {}, preview.get("rows") or {}
    by_kind = lambda kind: [(ref, c) for ref, c in cells.items() if c["kind"] == kind]  # noqa: E731
    out: list[str] = []

    trips = by_kind("trips")
    if trips:
        out.append(f"Trips — {blocks.get('card', 'card block')}, row {rows.get('card')}")
        for ref, c in trips:
            out.append(f"• {_SHIFT[c['shift']]}: {c['after']} ({ref}, {_state(c)}){_chp(c['after'], preview.get('report_trips'), c['shift'])}")

    readings = by_kind("hm")
    if readings:
        out.append(f"Hour meter — {blocks.get('hm', 'HM block')}, row {rows.get('hm')}")
        for shift in (1, 2, 3):
            parts = [
                f"{'opening' if c['key'].startswith('open') else 'closing'} {c['after']} ({ref}, {_state(c)})"
                for ref, c in readings
                if c["shift"] == shift
            ]
            if parts:
                hours = ((preview.get("hours") or {}).get(str(shift)) or {}).get("hours")
                out.append(f"• {_SHIFT[shift]}: " + " · ".join(parts) + (f" → {hours} h" if hours is not None else ""))

    refills = preview.get("refills") or []
    if refills:
        out.append(f"Diesel refills — {blocks.get('log', 'refill log')}")
        for r in refills:
            if r["state"] == "already":
                out.append(f"• {r['litres']} L at HM {r['hm']}: already logged")
            else:
                avg = f" · avg {r['avg']} L/h" if r.get("avg") else " · avg: no earlier refill to compare"
                out.append(f"• {r['litres']} L at HM {r['hm']} → new row {r['row']}{avg}")

    for ref, c in by_kind("hsd"):
        parts = []
        if c["added"]:
            parts.append("adding " + " + ".join(c["added"]))
        if c["already"]:
            parts.append(" + ".join(c["already"]) + " already in it")
        out.append(f"HSD Fuel ({ref}): {', '.join(parts)} → {_litres(c['existing'] + c['added'])}")
    return out


def render_card_summary(card: CardEntry, *, ttl_minutes: int, footer: str | None = None) -> str:
    head = f"🗂 <b>{loader_label(card.loader_number)} · {fmt_date(card.entry_date)}</b> (#{card.id})"
    if card.worksheet_name:
        head += f" · '{h(card.worksheet_name)}'"
    out = [head, ""]
    out += [h(line) for line in _sections(card)]
    warnings = (card.preview or {}).get("warnings") or []
    if warnings and card.status in (CardStatus.PENDING_CONFIRMATION, *_AFTER_CONFIRM):
        out += [""] + [f"⚠️ {h(w)}" for w in warnings]
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
    where = f"{loader_label(card.loader_number)} · {fmt_date(card.entry_date)} · '{h(card.worksheet_name)}'"
    if card.status != CardStatus.UPDATED:
        icon = "⚠️" if card.status == CardStatus.CONFLICT else "❌"
        return f"{icon} <b>Card #{card.id} not saved</b>\n{where}\n{h(card.error_message or card.status.value)}"
    preview = card.preview or {}
    lines = [f"✅ <b>Saved</b> card #{card.id}", where]
    for ref, c in (preview.get("cells") or {}).items():
        if c["before"] == c["after"]:
            continue
        if c["kind"] == "trips":
            lines.append(f"• {_SHIFT[c['shift']]} shift trips {c['after']} ({ref})")
        elif c["kind"] == "hm":
            what = "opening" if c["key"].startswith("open") else "closing"
            lines.append(f"• {_SHIFT[c['shift']]} shift {what} HM {c['after']} ({ref})")
        elif c["kind"] == "hsd":
            lines.append(f"• HSD Fuel {h(_litres(c['existing'] + c['added']))} ({ref})")
    for r in preview.get("refills") or []:
        if r["state"] == "new":
            avg = f", avg {r['avg']} L/h" if r.get("avg") else ""
            lines.append(f"• Refill {r['litres']} L at HM {r['hm']} → row {r['row']}{avg}")
    return "\n".join(lines)


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
                f"⚠️ Could not read this card message:\n{errors}\n\n{CARD_FORMAT_HELP}",
                reply_to_message_id=message.message_id,
            )
            return UpdateStatus.PROCESSED
        with db.session() as session:
            card = CardEntry(
                loader_number=parsed.loader_number,
                entry_date=parsed.entry_date,
                trips={str(k): v for k, v in parsed.trips.items()},
                hsd_fills=[str(f) for f in parsed.hsd_fills],
                hm_readings={f"o{k}": str(v) for k, v in parsed.opening.items()}
                | {f"c{k}": str(v) for k, v in parsed.closing.items()},
                refills=[{"litres": str(r.litres), "hm": None if r.hm is None else str(r.hm)} for r in parsed.refills],
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
                hm=card.hm_readings,
                refills=card.refills,
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
            telegram.answer_callback_query(query.id, "Card entry not found.")
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
            telegram.answer_callback_query(query.id, "Card entry not found.")
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
