"""Background worker: executes durable jobs from the database queue.

Runs as a thread inside the web process (RUN_WORKER_IN_PROCESS=true, the default) or as its own
process: ``python -m app.worker.runner``. Several workers may run against the same database.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from datetime import timedelta

from app.bot.formatting import render_summary
from app.container import AppContainer
from app.database.models import RecordStatus, ReportStatus, utcnow
from app.database.repositories import ReportRepository
from app.services.retry import backoff_delay
from app.services.telegram_service import TelegramApiError
from app.worker import job_queue
from app.worker.handlers import HANDLERS
from app.worker.job_queue import ClaimedJob, RetryJob

log = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 60.0


class Worker:
    def __init__(self, container: AppContainer, *, worker_id: str | None = None) -> None:
        self.container = container
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_maintenance = 0.0
        self.last_heartbeat: float | None = None
        container.add_wake_listener(self._wake.set)

    # --- Lifecycle --------------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run_forever, name="report-worker", daemon=True)
        self._thread.start()
        log.info("worker started", extra={"worker_id": self.worker_id})

    def stop(self, timeout: float = 30.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        log.info("worker stopped", extra={"worker_id": self.worker_id})

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_forever(self) -> None:
        poll = self.container.settings.worker_poll_interval_seconds
        while not self._stop.is_set():
            self.last_heartbeat = time.time()
            try:
                self._maybe_maintain()
                worked = self.run_once()
            except Exception:  # never let the loop die
                log.exception("worker loop error")
                worked = False
            if not worked:
                self._wake.wait(poll)
                self._wake.clear()

    # --- Work -------------------------------------------------------------------------------------

    def run_once(self) -> bool:
        job = job_queue.claim_next(self.container.db, self.worker_id)
        if job is None:
            return False
        self._execute(job)
        return True

    def run_until_idle(self, max_jobs: int = 100) -> int:
        """Process every runnable job (used by tests and one-off maintenance)."""
        count = 0
        while count < max_jobs and self.run_once():
            count += 1
        return count

    def _execute(self, job: ClaimedJob) -> None:
        handler = HANDLERS.get(job.job_type)
        context = {"job_id": job.id, "job_type": job.job_type, "attempt": job.attempts}
        if handler is None:
            job_queue.fail(self.container.db, job, f"unknown job type {job.job_type}", delay_seconds=0)
            return
        started = time.monotonic()
        try:
            handler(self.container, job)
        except RetryJob as exc:
            delay = exc.delay_seconds if exc.delay_seconds is not None else self._retry_delay(job)
            status = job_queue.fail(self.container.db, job, str(exc), delay_seconds=delay)
            log.warning("job will retry", extra={**context, "reason": str(exc), "delay_s": round(delay, 1), "status": status.value})
        except Exception as exc:
            status = job_queue.fail(self.container.db, job, f"{type(exc).__name__}: {exc}", delay_seconds=self._retry_delay(job))
            log.exception("job failed", extra={**context, "status": status.value})
        else:
            job_queue.complete(self.container.db, job.id)
            log.info("job done", extra={**context, "duration_ms": int((time.monotonic() - started) * 1000)})

    def _retry_delay(self, job: ClaimedJob) -> float:
        base = self.container.settings.job_retry_base_seconds
        return base * (2 ** (job.attempts - 1)) + backoff_delay(1, base=base)

    # --- Maintenance --------------------------------------------------------------------------------

    def _maybe_maintain(self) -> None:
        now = time.monotonic()
        if now - self._last_maintenance < MAINTENANCE_INTERVAL_SECONDS:
            return
        self._last_maintenance = now
        self.maintain()

    def maintain(self) -> None:
        requeued = job_queue.requeue_stale(self.container.db, self.container.settings.job_lease_seconds)
        if requeued:
            log.warning("re-queued stale jobs", extra={"count": requeued})
        expire_stale_confirmations(self.container)


def expire_stale_confirmations(container: AppContainer) -> int:
    """Expire summaries nobody confirmed in time and remove their buttons."""
    cutoff = utcnow() - timedelta(minutes=container.settings.confirmation_ttl_minutes)
    expired: list[tuple[int, int, int | None]] = []
    with container.db.session() as session:
        repo = ReportRepository(session)
        for report in repo.stale_pending_confirmation(cutoff):
            if repo.transition_report(report.id, [ReportStatus.PENDING_CONFIRMATION], ReportStatus.EXPIRED, completed_at=utcnow()):
                for record in report.records:
                    if record.processing_status == RecordStatus.READY:
                        record.processing_status = RecordStatus.CANCELLED
                expired.append((report.id, report.chat_id, report.summary_message_id))
    for report_id, chat_id, message_id in expired:
        if message_id is None:
            continue
        with container.db.session() as session:
            report = ReportRepository(session).get(report_id)
            text = render_summary(report, ttl_minutes=container.settings.confirmation_ttl_minutes)
        try:
            container.telegram.edit_message_text(
                chat_id, message_id, text.rstrip() + "\n⌛ Expired without confirmation — no spreadsheet was changed."
            )
        except TelegramApiError as exc:
            log.warning("could not edit expired summary", extra={"report_id": report_id, "error": str(exc)})
    return len(expired)


def main() -> None:  # pragma: no cover - process entry point
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    container = AppContainer.from_settings(settings)
    container.init()
    worker = Worker(container)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker.start()
    stop.wait()
    worker.stop()
    container.close()


if __name__ == "__main__":  # pragma: no cover
    main()
