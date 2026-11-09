"""Inline-button actions: Confirm/Cancel a report, Approve/Reject a correction.

Every state change is a compare-and-set transition, so double taps, two admins pressing at once
or Telegram re-delivering a callback cannot apply a report twice.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from app.bot.formatting import fmt_shift_values, h, loader_label, render_summary
from app.container import AppContainer
from app.database.models import RecordStatus, ReportStatus, User, utcnow
from app.database.repositories import ReportRepository, UserRepository, audit
from app.schemas.report import SHIFT_LABELS
from app.schemas.telegram import TgCallbackQuery
from app.services.calculations import ShiftValues
from app.services.telegram_service import TelegramApiError, inline_keyboard
from app.worker.job_queue import JobType, enqueue

log = logging.getLogger(__name__)


def handle_callback(container: AppContainer, query: TgCallbackQuery) -> None:
    action, _, ident = (query.data or "").partition(":")
    if not ident.isdigit():
        container.telegram.answer_callback_query(query.id, "Unknown action")
        return
    with container.db.session() as session:
        user = container.auth.resolve_user(session, query.from_user)
    if user is None:
        container.telegram.answer_callback_query(query.id, "⛔ You are not authorised.", show_alert=True)
        return
    target = int(ident)
    if action == "rc":
        confirm_report(container, query, user, target)
    elif action == "rx":
        cancel_report(container, query, user, target)
    elif action in ("ca", "cr"):
        if not container.auth.is_admin(user):
            container.telegram.answer_callback_query(query.id, "⛔ Administrators only.", show_alert=True)
            return
        ok, text = (approve_correction if action == "ca" else reject_correction)(container, user, target)
        container.telegram.answer_callback_query(query.id, text[:200])
        if ok and query.message:
            _edit_quietly(container, query.message.chat.id, query.message.message_id, h(text))
    else:
        container.telegram.answer_callback_query(query.id, "Unknown action")


def _edit_quietly(container: AppContainer, chat_id: int, message_id: int, text: str) -> None:
    try:
        container.telegram.edit_message_text(chat_id, message_id, text)
    except TelegramApiError as exc:
        log.warning("could not edit message", extra={"chat_id": chat_id, "error": str(exc)})


def _summary_with_footer(container: AppContainer, report_id: int, footer: str) -> str:
    with container.db.session() as session:
        report = ReportRepository(session).get(report_id)
        return render_summary(report, ttl_minutes=container.settings.confirmation_ttl_minutes).rsplit("\n", 1)[0] + "\n" + footer


def confirm_report(container: AppContainer, query: TgCallbackQuery, user: User, report_id: int) -> None:
    telegram = container.telegram
    is_admin = container.auth.is_admin(user)
    ttl = timedelta(minutes=container.settings.confirmation_ttl_minutes)
    awaiting: list[int] = []
    outcome = "confirmed"
    with container.db.session() as session:
        repo = ReportRepository(session)
        report = repo.get(report_id)
        if report is None:
            telegram.answer_callback_query(query.id, "Report not found.")
            return
        if report.telegram_user_id != user.telegram_user_id and not is_admin:
            telegram.answer_callback_query(
                query.id, "Only the person who sent this report (or an admin) can confirm it.", show_alert=True
            )
            return
        if report.status != ReportStatus.PENDING_CONFIRMATION:
            telegram.answer_callback_query(query.id, f"Already handled ({report.status.value.lower()}).")
            return
        if utcnow() - report.created_at > ttl:
            if repo.transition_report(report_id, [ReportStatus.PENDING_CONFIRMATION], ReportStatus.EXPIRED, completed_at=utcnow()):
                for record in report.records:
                    if record.processing_status == RecordStatus.READY:
                        record.processing_status = RecordStatus.CANCELLED
            outcome = "expired"
        elif not repo.transition_report(
            report_id,
            [ReportStatus.PENDING_CONFIRMATION],
            ReportStatus.CONFIRMED,
            confirmed_by=user.telegram_user_id,
            confirmed_at=utcnow(),
        ):
            telegram.answer_callback_query(query.id, "Already handled.")
            return
        else:
            queued = False
            for record in report.records:
                if record.processing_status != RecordStatus.READY:
                    continue
                if record.requires_approval and not is_admin:
                    record.processing_status = RecordStatus.AWAITING_APPROVAL
                    awaiting.append(record.id)
                else:
                    record.processing_status = RecordStatus.QUEUED
                    queued = True
            if queued:
                enqueue(session, JobType.APPLY_REPORT, {"report_id": report_id}, max_attempts=container.settings.job_max_attempts)
            else:
                report.status = ReportStatus.AWAITING_APPROVAL
            audit(
                session,
                "report_confirmed",
                actor=user.telegram_user_id,
                entity_type="report",
                entity_id=report_id,
                awaiting_approval=awaiting,
            )

    if query.message:
        if outcome == "expired":
            footer = "⌛ This confirmation expired — please forward the report again."
        else:
            footer = f"✅ Confirmed by {h(user.display_name)} — updating spreadsheets…"
            if awaiting:
                footer += f"\n⏳ {len(awaiting)} correction(s) sent to administrators for approval."
        _edit_quietly(container, query.message.chat.id, query.message.message_id, _summary_with_footer(container, report_id, footer))
    telegram.answer_callback_query(query.id, "Expired." if outcome == "expired" else "Updating spreadsheets…")
    if awaiting:
        notify_admins_for_approval(container, awaiting)
    container.wake_worker()


def cancel_report(container: AppContainer, query: TgCallbackQuery, user: User, report_id: int) -> None:
    telegram = container.telegram
    with container.db.session() as session:
        repo = ReportRepository(session)
        report = repo.get(report_id)
        if report is None:
            telegram.answer_callback_query(query.id, "Report not found.")
            return
        if report.telegram_user_id != user.telegram_user_id and not container.auth.is_admin(user):
            telegram.answer_callback_query(query.id, "Only the sender or an admin can cancel this report.", show_alert=True)
            return
        if not repo.transition_report(report_id, [ReportStatus.PENDING_CONFIRMATION], ReportStatus.CANCELLED, completed_at=utcnow()):
            telegram.answer_callback_query(query.id, f"Already handled ({report.status.value.lower()}).")
            return
        for record in report.records:
            if record.processing_status == RecordStatus.READY:
                record.processing_status = RecordStatus.CANCELLED
        audit(session, "report_cancelled", actor=user.telegram_user_id, entity_type="report", entity_id=report_id)
    if query.message:
        footer = f"✖️ Cancelled by {h(user.display_name)}. No spreadsheet was changed."
        _edit_quietly(container, query.message.chat.id, query.message.message_id, _summary_with_footer(container, report_id, footer))
    telegram.answer_callback_query(query.id, "Cancelled.")


# --- Corrections that need an administrator --------------------------------------------------


def notify_admins_for_approval(container: AppContainer, record_ids: list[int]) -> None:
    with container.db.session() as session:
        repo = ReportRepository(session)
        admin_ids = {u.telegram_user_id for u in UserRepository(session).list_admins()} | set(container.settings.admin_ids)
        messages = []
        for record_id in record_ids:
            record = repo.get_record(record_id)
            report = record.report
            before = ShiftValues.from_dict((record.preview_values or {}).get("before"))
            planned = ShiftValues.from_dict((record.preview_values or {}).get("planned"))
            text = (
                f"⏳ <b>Correction needs approval</b> (record {record.id}, report #{report.id})\n"
                f"{loader_label(record.loader_number)} · {report.report_date:%d-%m-%Y} · {SHIFT_LABELS[report.shift]}\n"
                f"'{h(record.worksheet_name)}' row {record.sheet_row}\n"
                f"Current: {fmt_shift_values(before)}\nNew: {fmt_shift_values(planned)}\n"
                f"Submitted by <code>{report.telegram_user_id}</code>"
            )
            keyboard = inline_keyboard([[("✅ Approve", f"ca:{record.id}"), ("⛔ Reject", f"cr:{record.id}")]])
            messages.append((text, keyboard))
    for admin_id in admin_ids:
        for text, keyboard in messages:
            try:
                container.telegram.send_message(admin_id, text, reply_markup=keyboard)
            except TelegramApiError as exc:
                # Usually: the admin never opened a private chat with the bot.
                log.warning("could not notify admin", extra={"admin_id": admin_id, "error": str(exc)})


def approve_correction(container: AppContainer, admin: User, record_id: int) -> tuple[bool, str]:
    with container.db.session() as session:
        repo = ReportRepository(session)
        record = repo.get_record(record_id)
        if record is None:
            return False, f"Record {record_id} not found."
        label = loader_label(record.loader_number)
        if not repo.transition_record(
            record_id, [RecordStatus.AWAITING_APPROVAL], RecordStatus.QUEUED, approved_by=admin.telegram_user_id
        ):
            return False, f"{label} (record {record_id}) is not awaiting approval ({record.processing_status.value})."
        repo.transition_report(record.report_id, [ReportStatus.AWAITING_APPROVAL], ReportStatus.CONFIRMED)
        enqueue(
            session,
            JobType.APPLY_REPORT,
            {"report_id": record.report_id, "record_ids": [record_id], "reason": "correction approved"},
            max_attempts=container.settings.job_max_attempts,
        )
        audit(session, "correction_approved", actor=admin.telegram_user_id, entity_type="loader_report_record", entity_id=record_id)
    container.wake_worker()
    return True, f"✅ Approved by {admin.display_name}: {label} correction is being written."


def reject_correction(container: AppContainer, admin: User, record_id: int) -> tuple[bool, str]:
    with container.db.session() as session:
        repo = ReportRepository(session)
        record = repo.get_record(record_id)
        if record is None:
            return False, f"Record {record_id} not found."
        label = loader_label(record.loader_number)
        if not repo.transition_record(
            record_id,
            [RecordStatus.AWAITING_APPROVAL],
            RecordStatus.REJECTED,
            approved_by=admin.telegram_user_id,
            error_code="CORRECTION_REJECTED",
            error_message="Correction rejected by an administrator",
        ):
            return False, f"{label} (record {record_id}) is not awaiting approval ({record.processing_status.value})."
        report_id, chat_id = record.report_id, record.report.chat_id
        summary_id = record.report.summary_message_id
        audit(session, "correction_rejected", actor=admin.telegram_user_id, entity_type="loader_report_record", entity_id=record_id)
    container.processor.finalize_report(report_id)
    try:
        container.telegram.send_message(
            chat_id,
            f"⛔ The correction for {label} (report #{report_id}) was rejected by an administrator. "
            "The spreadsheet was not changed.",
            reply_to_message_id=summary_id,
        )
    except TelegramApiError as exc:
        log.warning("could not notify submitter", extra={"error": str(exc)})
    return True, f"⛔ Rejected by {admin.display_name}: {label} correction discarded."
