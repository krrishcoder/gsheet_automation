"""Incoming report messages: parse -> validate -> preview against sheets -> confirmation summary."""

from __future__ import annotations

import logging
import re
from decimal import Decimal

from app.bot.formatting import h, render_summary
from app.container import AppContainer
from app.database.models import LoaderReportRecord, OperationalReport, ReportStatus, UpdateStatus
from app.database.repositories import ReportRepository, audit
from app.schemas.telegram import TgMessage
from app.services.report_parser import ReportParseError, parse_report
from app.services.telegram_service import inline_keyboard
from app.services.validation_service import validate_report

log = logging.getLogger(__name__)

_REPORT_HINT_RE = re.compile(r"\b(loader|trips?|qty|shift)\b", re.I)

FORMAT_HINT = (
    "Expected format:\n"
    "<code>DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 01\nTrip:- 42 Qty:-1340.840</code>\n\n"
    "Trips / hour meter / diesel for a loader? Start with <code>L11 10-11</code> — see /card_format"
)


def confirmation_keyboard(report_id: int) -> dict:
    return inline_keyboard([[("✅ Confirm Update", f"rc:{report_id}"), ("✖️ Cancel", f"rx:{report_id}")]])


def handle_report_message(container: AppContainer, update_id: int, message: TgMessage) -> UpdateStatus:
    db, auth, telegram = container.db, container.auth, container.telegram
    chat = message.chat
    text = message.content or ""

    with db.session() as session:
        if not auth.chat_accepts_reports(session, chat):
            return UpdateStatus.IGNORED
        user = auth.resolve_user(session, message.from_user)
        existing = ReportRepository(session).get_by_update(update_id)
        if user is None:
            audit(
                session,
                "report_rejected_unauthorized",
                actor=message.from_user.id if message.from_user else None,
                entity_type="chat",
                entity_id=chat.id,
            )

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
        # Re-delivery of a job that already created this report (e.g. after a crash).
        if existing.summary_message_id is not None:
            return UpdateStatus.PROCESSED
        report_id = existing.id
    else:
        try:
            parsed = parse_report(text)
        except ReportParseError as exc:
            if chat.is_private or _REPORT_HINT_RE.search(text):
                errors = "\n".join(f"• {h(e)}" for e in exc.errors)
                telegram.send_message(
                    chat.id,
                    f"⚠️ Could not read this as a loader report:\n{errors}\n\n{FORMAT_HINT}",
                    reply_to_message_id=message.message_id,
                )
            return UpdateStatus.PROCESSED

        issues = validate_report(
            parsed, today=container.today(), tolerance_mt=Decimal(container.settings.totals_tolerance_mt)
        )
        with db.session() as session:
            report = OperationalReport(
                report_date=parsed.report_date,
                shift=parsed.shift,
                telegram_user_id=user.telegram_user_id,
                chat_id=chat.id,
                source_message_id=message.message_id,
                source_update_id=update_id,
                raw_text=text,
                parsed_data=parsed.model_dump(mode="json"),
                validation_issues=[i.model_dump(mode="json") for i in issues],
                status=ReportStatus.PREVIEWING,
            )
            report.records = [
                LoaderReportRecord(loader_number=e.loader_number, trips=e.trips, quantity_mt=e.quantity_mt)
                for e in parsed.loaders
            ]
            session.add(report)
            session.flush()
            report_id = report.id
            audit(
                session,
                "report_received",
                actor=user.telegram_user_id,
                entity_type="report",
                entity_id=report_id,
                date=parsed.report_date,
                shift=parsed.shift,
                loaders=[e.loader_number for e in parsed.loaders],
                forwarded=message.is_forwarded,
            )
        log.info(
            "report parsed",
            extra={"report_id": report_id, "update_id": update_id, "loaders": [e.loader_number for e in parsed.loaders]},
        )

    container.processor.preview_report(report_id)
    with db.session() as session:
        report = ReportRepository(session).get(report_id)
        text_out = render_summary(report, ttl_minutes=container.settings.confirmation_ttl_minutes)
        pending = report.status == ReportStatus.PENDING_CONFIRMATION

    sent = telegram.send_message(
        chat.id,
        text_out,
        reply_markup=confirmation_keyboard(report_id) if pending else None,
        reply_to_message_id=message.message_id,
    )
    with db.session() as session:
        report = ReportRepository(session).get(report_id, with_records=False)
        report.summary_message_id = sent.get("message_id") if isinstance(sent, dict) else None
    return UpdateStatus.PROCESSED
