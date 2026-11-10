"""The month summary for the boss: built from the sheet after an update, sent to the person who
updated it with a button that opens WhatsApp with the summary typed (one tap, then Send)."""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from app.bot.formatting import h
from app.container import AppContainer
from app.database.repositories import SummaryRecipientRepository
from app.schemas.report import SHIFT_LABELS
from app.services.google_sheets_service import SheetsError
from app.services.retry import backoff_delay
from app.services.summary_service import render_summary, whatsapp_link
from app.worker.job_queue import ClaimedJob, JobType, RetryJob, enqueue

log = logging.getLogger(__name__)


def summary_message(container: AppContainer, loaders: list[int], year: int, month: int, *, note: str | None = None):
    """(Telegram text, reply markup), or None when no loader could be summarised.
    Raises SheetsError when Google Sheets is temporarily unavailable."""
    summaries = container.summaries.build(loaders, year, month)
    if not summaries:
        return None
    text = render_summary(summaries, note=note)
    with container.db.session() as session:
        recipients = [(r.name, r.phone) for r in SummaryRecipientRepository(session).list_all()]
    if recipients:
        buttons = [[{"text": f"📲 Send to {name or '+' + phone} on WhatsApp", "url": whatsapp_link(text, phone)}] for name, phone in recipients]
        hint = ""
    else:
        buttons = [[{"text": "📲 Share on WhatsApp", "url": whatsapp_link(text)}]]
        hint = "\n\n<i>Admin: /boss_add &lt;WhatsApp number&gt; &lt;name&gt; to open the boss's chat directly.</i>"
    body = f"📊 <b>Month summary</b> — tap the button, then Send in WhatsApp.\n\n{h(text)}{hint}"
    return body, {"inline_keyboard": buttons}


def queue_summary(
    session: Session,
    container: AppContainer,
    *,
    chat_id: int,
    reply_to: int | None,
    loaders: list[int],
    day: date,
    note: str,
) -> None:
    if not container.settings.summary_after_update or not loaders:
        return
    enqueue(
        session,
        JobType.SEND_SUMMARY,
        {"chat_id": chat_id, "reply_to": reply_to, "loaders": loaders, "year": day.year, "month": day.month, "note": note},
        max_attempts=container.settings.job_max_attempts,
    )


def report_note(day: date, shift: int) -> str:
    return f"Updated: {day:%d-%m} {SHIFT_LABELS[shift]}"


def send_summary_job(container: AppContainer, job: ClaimedJob) -> None:
    p = job.payload
    try:
        built = summary_message(container, list(p["loaders"]), int(p["year"]), int(p["month"]), note=p.get("note"))
    except SheetsError as exc:
        if not job.is_last_attempt:
            base = container.settings.job_retry_base_seconds
            raise RetryJob(exc.message, delay_seconds=base * (2 ** (job.attempts - 1)) + backoff_delay(1, base=base))
        container.telegram.send_message(p["chat_id"], f"⚠️ Could not read the sheet for the month summary: {h(exc.message)}")
        return
    if built is None:
        return
    text, markup = built
    container.telegram.send_message(p["chat_id"], text, reply_markup=markup, reply_to_message_id=p.get("reply_to"))
    log.info("month summary sent", extra={"chat_id": p["chat_id"], "loaders": p["loaders"]})
