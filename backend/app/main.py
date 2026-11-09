"""FastAPI application. Production: ``uvicorn app.main:app --host 0.0.0.0 --port 8000``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.telegram_webhook import build_webhook_router
from app.container import AppContainer
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.worker.runner import Worker

log = logging.getLogger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    container: AppContainer | None = None,
    start_worker: bool | None = None,
) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container is None:
            configure_logging(settings.log_level, settings.log_format)
        c = container or AppContainer.from_settings(settings)
        c.init()
        app.state.container = c
        run_worker = settings.run_worker_in_process if start_worker is None else start_worker
        worker = Worker(c) if run_worker else None
        app.state.worker = worker
        if worker:
            worker.start()
        log.info("application started", extra={"worker_in_process": bool(worker), "env": settings.app_env})
        try:
            yield
        finally:
            if worker:
                worker.stop()
            if container is None:
                c.close()

    docs = "/docs" if settings.is_development else None
    app = FastAPI(title="Loader Report Bot", version="1.0.0", lifespan=lifespan, docs_url=docs, redoc_url=None, openapi_url="/openapi.json" if docs else None)
    app.include_router(build_webhook_router(settings.webhook_path))
    app.include_router(health_router)

    @app.get("/", include_in_schema=False)
    def root() -> dict[str, str]:
        return {"service": "loader-report-bot", "status": "ok"}

    return app


app = create_app()
