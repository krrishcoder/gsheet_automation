"""Dependency wiring. Tests build an AppContainer with fake Telegram/Sheets clients."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.database.database import Database
from app.services.authorization_service import AuthorizationService
from app.services.card_service import CardService
from app.services.google_sheets_service import GoogleSheetsClient, SheetsClient
from app.services.locks import LockManager
from app.services.report_processor import ReportProcessor
from app.services.summary_service import SummaryService
from app.services.telegram_service import TelegramBotApi, TelegramClient


class AppContainer:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        telegram: TelegramClient,
        sheets: SheetsClient,
        *,
        locks: LockManager | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.telegram = telegram
        self.sheets = sheets
        self.auth = AuthorizationService(settings)
        self.locks = locks or LockManager(
            db, ttl_seconds=settings.sheet_lock_ttl_seconds, wait_seconds=settings.sheet_lock_wait_seconds
        )
        self.processor = ReportProcessor(db, sheets, self.locks, settings, self.auth)
        self.cards = CardService(db, sheets, self.locks, settings, self.auth)
        self.summaries = SummaryService(db, sheets, settings)
        self._wake_listeners: list[Callable[[], None]] = []

    @classmethod
    def from_settings(cls, settings: Settings) -> "AppContainer":
        missing = settings.missing_runtime_settings()
        if missing:
            raise RuntimeError("Missing required configuration: " + ", ".join(missing))
        db = Database(settings.database_url)
        telegram = TelegramBotApi(settings.telegram_bot_token, base_url=settings.telegram_api_base_url)
        sheets = GoogleSheetsClient.from_settings(settings)
        return cls(settings, db, telegram, sheets)

    def init(self) -> None:
        self.db.create_all()
        with self.db.session() as session:
            self.auth.bootstrap_admins(session)

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.settings.timezone)).date()

    # The webhook calls wake_worker() after persisting an update so the in-process worker
    # starts immediately instead of waiting for its next poll.
    def add_wake_listener(self, listener: Callable[[], None]) -> None:
        self._wake_listeners.append(listener)

    def wake_worker(self) -> None:
        for listener in self._wake_listeners:
            listener()

    def close(self) -> None:
        close = getattr(self.telegram, "close", None)
        if callable(close):
            close()
        self.db.dispose()
