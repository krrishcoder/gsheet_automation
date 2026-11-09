"""Mutual exclusion for read-modify-write cycles on a worksheet.

Two layers:
* an in-process ``threading.Lock`` per resource (cheap, covers worker threads), and
* a database lease (``resource_locks`` row with an expiry) so separate processes - e.g. the web
  container and a standalone worker - also serialise. A crashed holder's lease simply expires.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.database.database import Database
from app.database.models import ResourceLock, utcnow


class LockTimeout(Exception):
    pass


class LockManager:
    def __init__(self, db: Database, *, ttl_seconds: int = 300, wait_seconds: float = 120.0) -> None:
        self._db = db
        self._ttl = timedelta(seconds=ttl_seconds)
        self._wait = wait_seconds
        self._owner_prefix = f"{socket.gethostname()}:{os.getpid()}"
        self._local_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _local_lock(self, name: str) -> threading.Lock:
        with self._guard:
            return self._local_locks.setdefault(name, threading.Lock())

    def _try_acquire(self, name: str, owner: str) -> bool:
        now = utcnow()
        with self._db.session() as session:
            result = session.execute(
                update(ResourceLock)
                .where(ResourceLock.name == name, ResourceLock.expires_at < now)
                .values(owner=owner, acquired_at=now, expires_at=now + self._ttl)
            )
            if result.rowcount == 1:
                return True
        try:
            with self._db.session() as session:
                session.add(ResourceLock(name=name, owner=owner, acquired_at=now, expires_at=now + self._ttl))
            return True
        except IntegrityError:
            return False

    def _release(self, name: str, owner: str) -> None:
        with self._db.session() as session:
            session.execute(delete(ResourceLock).where(ResourceLock.name == name, ResourceLock.owner == owner))

    @contextmanager
    def hold(self, name: str) -> Iterator[None]:
        local = self._local_lock(name)
        deadline = time.monotonic() + self._wait
        if not local.acquire(timeout=self._wait):
            raise LockTimeout(f"Timed out waiting for lock {name}")
        owner = f"{self._owner_prefix}:{uuid.uuid4().hex[:8]}"
        try:
            delay = 0.05
            while not self._try_acquire(name, owner):
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"Timed out waiting for lock {name}")
                time.sleep(delay)
                delay = min(delay * 2, 1.0)
            try:
                yield
            finally:
                self._release(name, owner)
        finally:
            local.release()
