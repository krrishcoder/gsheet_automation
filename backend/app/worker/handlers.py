"""Job handlers executed by the background worker."""

from __future__ import annotations

import logging

from app.bot.card_handler import send_card_result
from app.bot.dispatcher import dispatch_update
from app.bot.formatting import render_result
from app.container import AppContainer
from app.database.models import RecordStatus, TelegramUpdate, UpdateStatus, utcnow
from app.database.repositories import ReportRepository
from app.schemas.telegram import TgUpdate
from app.services.report_processor import IN_FLIGHT_STATUSES, TransientApplyError
from app.services.retry import backoff_delay
from app.services.telegram_service import TelegramApiError
from app.worker.job_queue import ClaimedJob, JobType, RetryJob

log = logging.getLogger(__name__)


def process_update_job(container: AppContainer, job: ClaimedJob) -> None:
    update_id = int(job.payload["update_id"])
    with container.db.session() as session:
        stored = session.get(TelegramUpdate, update_id)
        if stored is None or stored.processing_status in (UpdateStatus.PROCESSED, UpdateStatus.IGNORED):
            return
        stored.processing_status = UpdateStatus.PROCESSING
        payload = dict(stored.payload)
    try:
        status = dispatch_update(container, TgUpdate.model_validate(payload))
    except Exception as exc:
        with container.db.session() as session:
            stored = session.get(TelegramUpdate, update_id)
            stored.processing_status = UpdateStatus.FAILED
            stored.error_message = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    with container.db.session() as session:
        stored = session.get(TelegramUpdate, update_id)
        stored.processing_status = status
        stored.error_message = None
        stored.completed_at = utcnow()


def apply_report_job(container: AppContainer, job: ClaimedJob) -> None:
    report_id = int(job.payload["report_id"])
    only = set(job.payload.get("record_ids") or [])
    with container.db.session() as session:
        report = ReportRepository(session).get(report_id)
        if report is None:
            return
        record_ids = [
            r.id for r in report.records if r.processing_status in IN_FLIGHT_STATUSES and (not only or r.id in only)
        ]

    transient: list[int] = []
    for record_id in record_ids:
        try:
            container.processor.apply_record(record_id)
        except TransientApplyError:
            transient.append(record_id)
    if transient:
        if not job.is_last_attempt:
            base = container.settings.job_retry_base_seconds
            delay = base * (2 ** (job.attempts - 1)) + backoff_delay(1, base=base)
            raise RetryJob(f"{len(transient)} record(s) hit temporary errors", delay_seconds=delay)
        container.processor.fail_in_flight(
            transient, f"Google Sheets kept failing after {job.attempts} attempts; an admin can run /retry_report {report_id}"
        )

    container.processor.finalize_report(report_id)
    _send_result(container, report_id, only)


def _send_result(container: AppContainer, report_id: int, only: set[int]) -> None:
    with container.db.session() as session:
        report = ReportRepository(session).get(report_id)
        if not only and report.result_message_id is not None:
            return  # full result already delivered (job re-run after a crash)
        records = [r for r in report.records if r.id in only] if only else None
        title = "Follow-up update" if only else None
        text = render_result(report, records, title=title)
        chat_id, reply_to = report.chat_id, report.summary_message_id
        pending_approval = any(r.processing_status == RecordStatus.AWAITING_APPROVAL for r in report.records)
    try:
        sent = container.telegram.send_message(chat_id, text, reply_to_message_id=reply_to)
    except TelegramApiError as exc:
        log.error("could not deliver report result", extra={"report_id": report_id, "error": str(exc)})
        raise
    if not only:
        with container.db.session() as session:
            report = ReportRepository(session).get(report_id, with_records=False)
            report.result_message_id = sent.get("message_id") if isinstance(sent, dict) else 0
    log.info("report result sent", extra={"report_id": report_id, "awaiting_approval": pending_approval})


def apply_card_job(container: AppContainer, job: ClaimedJob) -> None:
    card_id = int(job.payload["card_id"])
    try:
        container.cards.apply(card_id)
    except TransientApplyError as exc:
        if not job.is_last_attempt:
            base = container.settings.job_retry_base_seconds
            raise RetryJob(str(exc), delay_seconds=base * (2 ** (job.attempts - 1)) + backoff_delay(1, base=base))
        container.cards.fail_in_flight(card_id, f"Google Sheets kept failing after {job.attempts} attempts; please send it again")
    send_card_result(container, card_id)


HANDLERS = {
    JobType.PROCESS_UPDATE: process_update_job,
    JobType.APPLY_REPORT: apply_report_job,
    JobType.APPLY_CARD: apply_card_job,
}
