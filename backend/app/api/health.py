"""GET /health — liveness/readiness for load balancers, Docker and uptime monitors."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.database.repositories import JobRepository

router = APIRouter()


def _collect(request: Request) -> tuple[int, dict]:
    container = request.app.state.container
    worker = getattr(request.app.state, "worker", None)
    body: dict = {"status": "ok"}
    try:
        container.db.ping()
        with container.db.session() as session:
            body["jobs"] = JobRepository(session).counts()
        body["database"] = "ok"
    except Exception as exc:  # pragma: no cover - depends on infrastructure failure
        body.update(status="error", database=f"error: {type(exc).__name__}")
        return 503, body

    if worker is None:
        body["worker"] = "external"
    else:
        stale_after = max(30.0, container.settings.worker_poll_interval_seconds * 10)
        age = None if worker.last_heartbeat is None else round(time.time() - worker.last_heartbeat, 1)
        healthy = worker.is_running and age is not None and age < stale_after
        body["worker"] = {"running": worker.is_running, "heartbeat_age_s": age}
        if not healthy:
            body["status"] = "degraded"
    if body["jobs"].get("DEAD"):
        body["status"] = "degraded"
    return 200, body


@router.get("/health", include_in_schema=False)
async def health(request: Request) -> JSONResponse:
    status_code, body = await run_in_threadpool(_collect, request)
    return JSONResponse(body, status_code=status_code)
