from __future__ import annotations

import itertools
import os
from datetime import date

import pytest

from app.container import AppContainer
from app.core.config import Settings
from app.database.database import Database
from app.database.models import Base, Loader, LoaderWorksheet, UserRole
from app.database.repositories import UserRepository
from app.api.telegram_webhook import persist_update
from app.schemas.telegram import TgUpdate
from app.worker.runner import Worker
from tests.fakes import FakeSheets, FakeTelegram, build_month_sheet

ADMIN_ID = 9001
USER_ID = 5001
OTHER_USER_ID = 5002
REPORT_DATE = date(2026, 11, 6)

SAMPLE_REPORT = """*LOADER REPORT - CHP*

*DATE*:- 06/11/2026

*SHIFT*:- 1st Shift

*LOADER - 01*
Trip:- 42 Qty:-1340.840

*LOADER - 05*
Trip:-43 Qty:-1411.930

*LOADER - 11*
Trip:- 21 Qty:-696.380

*PC - 15*
Trip:- Nil Qty:- Nil

*PC - 21*
Trip:- Nil Qty:- Nil

*Total Trip*:-106

*Total Qty*:-3449.150"""


def sheet_id(loader_number: int) -> str:
    return f"sheet-loader-{loader_number:02d}-abcdefghijklmnop"


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        app_env="test",
        telegram_bot_token="123456:TEST",
        telegram_webhook_secret="test-secret-token",
        admin_telegram_ids=str(ADMIN_ID),
        # e.g. TEST_DATABASE_URL=postgresql+psycopg://user:pw@localhost/loaderbot_test (wiped per test)
        database_url=os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/test.db",
        google_service_account_json="{}",
        job_retry_base_seconds=0.01,
        sheet_lock_wait_seconds=20,
        log_format="text",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def make_container(tmp_path):
    created: list[AppContainer] = []

    def factory(**overrides) -> AppContainer:
        settings = make_settings(tmp_path, **overrides)
        db = Database(settings.database_url)
        if os.environ.get("TEST_DATABASE_URL"):
            Base.metadata.drop_all(db.engine)
        container = AppContainer(settings, db, FakeTelegram(), FakeSheets())
        container.init()
        created.append(container)
        return container

    yield factory
    for c in created:
        c.db.dispose()


@pytest.fixture
def container(make_container) -> AppContainer:
    return make_container()


class Bot:
    """Drives the system the way Telegram would: webhook persistence + background worker."""

    _ids = itertools.count(1)

    def __init__(self, container: AppContainer) -> None:
        self.c = container
        self.telegram: FakeTelegram = container.telegram  # type: ignore[assignment]
        self.sheets: FakeSheets = container.sheets  # type: ignore[assignment]
        self.worker = Worker(container, worker_id="test-worker")

    def _deliver(self, payload: dict) -> None:
        persist_update(self.c, TgUpdate.model_validate(payload), payload)
        self.worker.run_until_idle()

    def send(self, text: str, *, user_id: int = USER_ID, chat_id: int | None = None, chat_type: str = "private") -> dict:
        n = next(self._ids)
        payload = {
            "update_id": 10_000 + n,
            "message": {
                "message_id": 100 + n,
                "date": 1_700_000_000,
                "chat": {"id": chat_id or user_id, "type": chat_type, "title": "Ops" if chat_type != "private" else None},
                "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}", "username": f"user{user_id}"},
                "text": text,
                "forward_origin": {"type": "hidden_user", "sender_user_name": "WhatsApp", "date": 1_700_000_000},
            },
        }
        before = len(self.telegram.sent)
        self._deliver(payload)
        new = self.telegram.sent[before:]
        return new[-1] if new else {}

    def press(self, data: str, *, user_id: int = USER_ID, message: dict | None = None) -> None:
        n = next(self._ids)
        msg = message or {"message_id": 1, "chat_id": user_id}
        payload = {
            "update_id": 10_000 + n,
            "callback_query": {
                "id": f"cb{n}",
                "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"},
                "data": data,
                "message": {"message_id": msg["message_id"], "date": 0, "chat": {"id": msg["chat_id"], "type": "private"}},
            },
        }
        self._deliver(payload)

    def confirm(self, summary: dict, *, user_id: int = USER_ID) -> dict:
        data = next(d for d in self.telegram.buttons(summary) if d.startswith("rc:"))
        before = len(self.telegram.sent)
        self.press(data, user_id=user_id, message=summary)
        new = self.telegram.sent[before:]
        return new[-1] if new else {}


@pytest.fixture
def bot(container) -> Bot:
    return Bot(container)


def add_loader(
    container: AppContainer,
    number: int,
    *,
    worksheet: str | None = "default",
    users: tuple[int, ...] = (USER_ID,),
    two_tier: bool = True,
    create_tab: bool = True,
    year: int = 2026,
    month: int = 11,
):
    """Register a loader + its spreadsheet (with a monthly tab) and grant users access."""
    tab_name = f"Loader {number:02d} nov 2026" if worksheet == "default" else worksheet
    sheets: FakeSheets = container.sheets  # type: ignore[assignment]
    sheets.add_spreadsheet(sheet_id(number), title=f"Loader {number:02d} production")
    ws = rows = None
    if create_tab and tab_name:
        ws, rows = build_month_sheet(tab_name, year, month, two_tier=two_tier, loader_number=number)
        sheets.add_worksheet(sheet_id(number), ws)
    with container.db.session() as session:
        loader = Loader(
            loader_number=number,
            spreadsheet_url=f"https://docs.google.com/spreadsheets/d/{sheet_id(number)}/edit",
            spreadsheet_id=sheet_id(number),
            is_active=True,
        )
        session.add(loader)
        session.flush()
        if worksheet:
            session.add(LoaderWorksheet(loader_id=loader.id, year=year, month=month, worksheet_name=tab_name))
        repo = UserRepository(session)
        for uid in users:
            user, _ = repo.get_or_create(uid, role=UserRole.USER)
            repo.grant_loader(user, loader, ADMIN_ID)
    return ws, rows
