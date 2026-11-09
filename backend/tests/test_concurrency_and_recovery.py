"""Concurrent writers, job queue semantics and crash recovery."""

import threading
from datetime import timedelta

from sqlalchemy import select, update

from app.database.models import Job, JobStatus, LoaderReportRecord, RecordStatus, utcnow
from app.services.locks import LockManager
from app.worker import job_queue
from app.worker.runner import Worker
from tests.conftest import REPORT_DATE, USER_ID, Bot, add_loader

SHIFT1 = "DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 21 Qty:- 696.380"
SHIFT2 = "DATE:- 06/11/2026\nSHIFT:- 2nd Shift\nLOADER - 11\nTrip:- 10 Qty:- 300.500"


def test_concurrent_updates_to_one_row_do_not_lose_data(bot, container):
    """Two workers apply different shifts of the same day at once. Without the sheet lock,
    both would read the empty row and the second write would compute day totals without the
    first shift (a lost update)."""
    from app.bot.confirmation_handler import confirm_report
    from app.database.repositories import UserRepository
    from app.schemas.telegram import TgCallbackQuery

    ws, rows = add_loader(container, 11)
    r = rows[REPORT_DATE]
    # Both summaries first (both previews see an empty row), then both confirmations,
    # without letting the worker run any apply job in between.
    summaries = [bot.send(text) for text in (SHIFT1, SHIFT2)]
    with container.db.session() as s:
        user = UserRepository(s).get_by_telegram_id(USER_ID)
    for summary in summaries:
        data = next(d for d in bot.telegram.buttons(summary) if d.startswith("rc:"))
        query = TgCallbackQuery.model_validate({"id": "x", "from": {"id": USER_ID, "first_name": "u"}, "data": data})
        confirm_report(container, query, user, int(data.split(":")[1]))
    with container.db.session() as s:
        assert s.query(Job).filter(Job.status == JobStatus.QUEUED).count() == 2

    container.sheets.read_delay = 0.3
    workers = [Worker(container, worker_id=f"w{i}") for i in range(2)]
    threads = [threading.Thread(target=w.run_until_idle) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert (ws.value(f"D{r}"), ws.value(f"E{r}"), ws.value(f"G{r}"), ws.value(f"H{r}")) == (21, 696.38, 10, 300.5)
    assert ws.value(f"M{r}") == 996.88
    assert ws.value(f"N{r}") == 31
    with container.db.session() as s:
        statuses = {rec.processing_status for rec in s.scalars(select(LoaderReportRecord))}
    assert statuses == {RecordStatus.UPDATED}


def test_lock_is_exclusive_across_managers(container):
    a = LockManager(container.db, ttl_seconds=30, wait_seconds=5)
    b = LockManager(container.db, ttl_seconds=30, wait_seconds=0.3)
    from app.services.locks import LockTimeout

    with a.hold("sheet:x:y"):
        try:
            with b.hold("sheet:x:y"):
                raise AssertionError("second manager acquired a held lock")
        except LockTimeout:
            pass
    with b.hold("sheet:x:y"):  # released -> available
        pass


def test_expired_lock_lease_is_taken_over(container):
    from app.database.models import ResourceLock

    with container.db.session() as s:
        s.add(ResourceLock(name="sheet:x:y", owner="dead-process", expires_at=utcnow() - timedelta(seconds=1)))
    with LockManager(container.db, wait_seconds=1).hold("sheet:x:y"):
        pass


def test_crash_after_sheet_write_is_recovered_idempotently(bot, container):
    ws, rows = add_loader(container, 11)
    r = rows[REPORT_DATE]
    ws.set(f"G{r}", 10)
    ws.set(f"H{r}", 300.5)
    summary = bot.send(SHIFT1)

    # Simulate: the worker wrote the sheet, then died before recording success.
    original_write = container.sheets.write_cells

    def write_then_crash(*args, **kwargs):
        original_write(*args, **kwargs)
        raise SystemExit("worker killed")

    container.sheets.write_cells = write_then_crash
    try:
        bot.confirm(summary)
    except SystemExit:
        pass
    container.sheets.write_cells = original_write
    with container.db.session() as s:
        record = s.scalars(select(LoaderReportRecord)).one()
        assert record.processing_status == RecordStatus.APPLYING
        assert record.previous_values["shifts"]["1"] == {"trips": None, "weight": None}
        # the job is still RUNNING with a lease from the dead worker
        s.execute(update(Job).where(Job.status == JobStatus.RUNNING).values(locked_at=utcnow() - timedelta(hours=1)))

    assert job_queue.requeue_stale(container.db, lease_seconds=60) == 1
    Bot(container).worker.run_until_idle()

    with container.db.session() as s:
        record = s.scalars(select(LoaderReportRecord)).one()
        assert record.processing_status == RecordStatus.UPDATED
        assert record.previous_values["shifts"]["1"] == {"trips": None, "weight": None}  # original kept
    assert (ws.value(f"D{r}"), ws.value(f"M{r}"), ws.value(f"N{r}")) == (21, 996.88, 31)


def test_job_queue_retry_and_dead_letter(container):
    with container.db.session() as s:
        job_queue.enqueue(s, "unknown_type", {}, max_attempts=2)
    worker = Worker(container, worker_id="w")
    assert worker.run_once()
    with container.db.session() as s:
        job = s.scalars(select(Job)).one()
        assert job.status == JobStatus.QUEUED and job.attempts == 1
        job.run_after = utcnow()
    assert worker.run_once()
    with container.db.session() as s:
        assert s.scalars(select(Job)).one().status == JobStatus.DEAD


def test_a_job_is_claimed_only_once(container):
    with container.db.session() as s:
        job_queue.enqueue(s, "x", {})
    claims = []
    barrier = threading.Barrier(4)

    def claim(i):
        barrier.wait()
        claims.append(job_queue.claim_next(container.db, f"w{i}"))

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(c is not None for c in claims) == 1
