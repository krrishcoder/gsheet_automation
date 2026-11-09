"""Database-backed job queue.

Jobs are rows in ``jobs``. Claiming uses a compare-and-set UPDATE (``WHERE status='QUEUED'``),
which is atomic on both SQLite and PostgreSQL, so several workers can poll safely. A job whose
worker died stays RUNNING until its lease expires and is then re-queued.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database.database import Database
from app.database.models import Job, JobStatus, utcnow


class JobType:
    PROCESS_UPDATE = "process_update"
    APPLY_REPORT = "apply_report"


class RetryJob(Exception):
    """Raised by a handler to re-run the job later (no stack trace logged)."""

    def __init__(self, message: str, *, delay_seconds: float | None = None) -> None:
        super().__init__(message)
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    job_type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


def enqueue(session: Session, job_type: str, payload: dict[str, Any], *, max_attempts: int = 6, delay_seconds: float = 0) -> Job:
    job = Job(
        job_type=job_type,
        payload=payload,
        status=JobStatus.QUEUED,
        max_attempts=max_attempts,
        run_after=utcnow() + timedelta(seconds=delay_seconds),
    )
    session.add(job)
    session.flush()
    return job


def claim_next(db: Database, worker_id: str) -> ClaimedJob | None:
    now = utcnow()
    with db.session() as session:
        candidates = session.scalars(
            select(Job.id)
            .where(Job.status == JobStatus.QUEUED, Job.run_after <= now)
            .order_by(Job.run_after, Job.id)
            .limit(5)
        ).all()
        for job_id in candidates:
            claimed = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .values(
                    status=JobStatus.RUNNING,
                    locked_by=worker_id,
                    locked_at=now,
                    attempts=Job.attempts + 1,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount == 1:
                job = session.get(Job, job_id)
                session.refresh(job)
                return ClaimedJob(job.id, job.job_type, dict(job.payload), job.attempts, job.max_attempts)
    return None


def complete(db: Database, job_id: int) -> None:
    with db.session() as session:
        session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=JobStatus.SUCCEEDED, finished_at=utcnow(), locked_by=None, last_error=None)
        )


def fail(db: Database, job: ClaimedJob, error: str, *, delay_seconds: float) -> JobStatus:
    """Re-queue with a delay, or mark DEAD once attempts are exhausted."""
    status = JobStatus.DEAD if job.is_last_attempt else JobStatus.QUEUED
    values: dict[str, Any] = {"status": status, "last_error": error[:4000], "locked_by": None}
    if status is JobStatus.QUEUED:
        values["run_after"] = utcnow() + timedelta(seconds=delay_seconds)
    else:
        values["finished_at"] = utcnow()
    with db.session() as session:
        session.execute(update(Job).where(Job.id == job.id).values(**values))
    return status


def requeue_stale(db: Database, lease_seconds: int) -> int:
    cutoff = utcnow() - timedelta(seconds=lease_seconds)
    stale = (Job.status == JobStatus.RUNNING) & (Job.locked_at < cutoff)
    with db.session() as session:
        # A job that keeps killing its worker must not loop forever.
        session.execute(
            update(Job)
            .where(stale, Job.attempts >= Job.max_attempts)
            .values(status=JobStatus.DEAD, locked_by=None, finished_at=utcnow(), last_error="lease expired; attempts exhausted")
        )
        result = session.execute(
            update(Job)
            .where(stale)
            .values(status=JobStatus.QUEUED, locked_by=None, run_after=utcnow(), last_error="lease expired; re-queued")
        )
        return result.rowcount
