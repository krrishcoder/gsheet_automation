"""POST /telegram/webhook — authenticate, persist, acknowledge.

The endpoint does no report processing itself. It stores the raw update (``update_id`` is the
primary key, so Telegram re-deliveries are recognised) together with a queued job in a single
transaction, wakes the worker and returns 200. If the process dies right after, the job is still
in the database and is processed after restart.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.container import AppContainer
from app.core.security import TELEGRAM_SECRET_HEADER, verify_webhook_secret
from app.database.models import TelegramUpdate, UpdateStatus
from app.schemas.telegram import TgUpdate
from app.worker.job_queue import JobType, enqueue

log = logging.getLogger(__name__)


def persist_update(container: AppContainer, update: TgUpdate, payload: dict[str, Any]) -> bool:
    """Store the update and its job atomically. Returns False for a duplicate delivery."""
    actor = update.actor
    chat = update.chat
    try:
        with container.db.session() as session:
            session.add(
                TelegramUpdate(
                    update_id=update.update_id,
                    update_type=update.kind,
                    chat_id=chat.id if chat else None,
                    telegram_user_id=actor.id if actor else None,
                    payload=payload,
                    processing_status=UpdateStatus.RECEIVED,
                )
            )
            session.flush()
            enqueue(
                session,
                JobType.PROCESS_UPDATE,
                {"update_id": update.update_id},
                max_attempts=container.settings.job_max_attempts,
            )
    except IntegrityError:
        return False
    return True


def build_webhook_router(path: str) -> APIRouter:
    router = APIRouter()

    @router.post(path, include_in_schema=False)
    async def telegram_webhook(request: Request) -> JSONResponse:
        container: AppContainer = request.app.state.container
        settings = container.settings
        if not verify_webhook_secret(request.headers.get(TELEGRAM_SECRET_HEADER), settings.telegram_webhook_secret):
            log.warning("webhook rejected: invalid secret token", extra={"client": request.client.host if request.client else None})
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_webhook_body_bytes:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        body = await request.body()
        if len(body) > settings.max_webhook_body_bytes:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        try:
            payload = json.loads(body)
            update = TgUpdate.model_validate(payload)
        except (ValueError, ValidationError):
            log.warning("webhook rejected: malformed update")
            return JSONResponse({"ok": False, "error": "malformed update"}, status_code=400)

        accepted = await run_in_threadpool(persist_update, container, update, payload)
        if accepted:
            container.wake_worker()
            log.info("update accepted", extra={"update_id": update.update_id, "kind": update.kind})
        else:
            log.info("duplicate update ignored", extra={"update_id": update.update_id})
        return JSONResponse({"ok": True, "duplicate": not accepted})

    return router
