from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database.models import Job, TelegramUpdate
from app.main import create_app
from tests.conftest import USER_ID

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _update(update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 7,
            "date": 0,
            "chat": {"id": USER_ID, "type": "private"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "U"},
            "text": "/start",
        },
    }


def _client(container):
    return TestClient(create_app(container=container, start_worker=False))


def _count(container, model):
    with container.db.session() as s:
        return s.scalar(select(func.count()).select_from(model))


def test_rejects_missing_or_wrong_secret(container):
    with _client(container) as client:
        assert client.post("/telegram/webhook", json=_update()).status_code == 401
        assert client.post("/telegram/webhook", json=_update(), headers={SECRET_HEADER: "nope"}).status_code == 401
    assert _count(container, TelegramUpdate) == 0


def test_accepts_and_persists_update_with_a_job(container):
    with _client(container) as client:
        response = client.post("/telegram/webhook", json=_update(), headers={SECRET_HEADER: "test-secret-token"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "duplicate": False}
    assert _count(container, TelegramUpdate) == 1
    with container.db.session() as s:
        job = s.scalars(select(Job)).one()
        assert job.payload == {"update_id": 1}
    assert container.telegram.sent == []  # processing happens in the worker, not in the request


def test_duplicate_delivery_is_ignored(container):
    with _client(container) as client:
        for _ in range(3):
            response = client.post("/telegram/webhook", json=_update(42), headers={SECRET_HEADER: "test-secret-token"})
            assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert _count(container, TelegramUpdate) == 1
    assert _count(container, Job) == 1


def test_malformed_and_oversized_bodies(container):
    headers = {SECRET_HEADER: "test-secret-token"}
    with _client(container) as client:
        assert client.post("/telegram/webhook", content=b"not json", headers=headers).status_code == 400
        assert client.post("/telegram/webhook", json={"no": "update_id"}, headers=headers).status_code == 400
        big = b"{" + b" " * 1_100_000 + b"}"
        assert client.post("/telegram/webhook", content=big, headers=headers).status_code == 413


def test_accepted_update_is_processed_by_the_worker(container):
    from app.worker.runner import Worker

    with _client(container) as client:
        client.post("/telegram/webhook", json=_update(5), headers={SECRET_HEADER: "test-secret-token"})
    Worker(container).run_until_idle()
    assert "Your Telegram user ID is <code>5001</code>" in container.telegram.sent[-1]["text"]
    with container.db.session() as s:
        assert s.get(TelegramUpdate, 5).processing_status.value == "PROCESSED"


def test_health(container):
    with _client(container) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok" and body["database"] == "ok" and body["worker"] == "external"
