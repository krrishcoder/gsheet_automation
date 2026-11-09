"""Google Sheets REST client and Telegram Bot API client, with mocked HTTP."""

import json

import httpx
import pytest

from app.schemas.sheets import CellWrite
from app.services.google_sheets_service import GoogleSheetsClient, SheetsError, a1, parse_grid_response
from app.services.telegram_service import TelegramApiError, TelegramBotApi


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = json.dumps(self._payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def client_with(responses):
    session = FakeSession(responses)
    creds = type("Creds", (), {"service_account_email": "bot@proj.iam.gserviceaccount.com"})()
    client = GoogleSheetsClient(creds, max_attempts=4, sleep=lambda _s: None, session_factory=lambda _c: session)
    return client, session


GRID_PAYLOAD = {
    "sheets": [
        {
            "properties": {"title": "Loader 11 nov 2026"},
            "merges": [{"startRowIndex": 1, "endRowIndex": 2, "startColumnIndex": 3, "endColumnIndex": 6}],
            "data": [
                {
                    "rowData": [
                        {},
                        {"values": [{}, {"formattedValue": "Date", "effectiveValue": {"stringValue": "Date"}},
                                    {}, {"formattedValue": "1st Shift", "effectiveValue": {"stringValue": "1st Shift"}}]},
                        {"values": [{}, {"formattedValue": "06-11-2026", "effectiveValue": {"numberValue": 46332}},
                                    {}, {"formattedValue": "21", "effectiveValue": {"numberValue": 21}},
                                    {"formattedValue": "696.380 MT", "effectiveValue": {"numberValue": 696.38}},
                                    {"formattedValue": "33.16", "effectiveValue": {"numberValue": 33.16},
                                     "userEnteredValue": {"formulaValue": "=E3/D3"}},
                                    {"effectiveValue": {"errorValue": {"type": "DIVIDE_BY_ZERO", "message": "#DIV/0!"}}}]},
                    ]
                }
            ],
        }
    ]
}


def test_grid_parsing_keeps_values_formats_formulas_and_merges():
    grid = parse_grid_response(GRID_PAYLOAD, "fallback")
    assert grid.title == "Loader 11 nov 2026"
    assert grid.cell(3, 2).effective == 46332 and grid.cell(3, 2).formatted == "06-11-2026"
    assert grid.cell(3, 5).effective == 696.38 and grid.cell(3, 5).formatted == "696.380 MT"
    assert grid.cell(3, 6).formula == "=E3/D3"
    assert grid.cell(3, 7).error == "#DIV/0!"
    assert grid.label(2, 5) == "1st Shift"  # merged D2:F2
    assert grid.cell(99, 99).is_empty


def test_read_grid_requests_only_needed_fields_and_quotes_the_tab():
    client, session = client_with([FakeResponse(200, GRID_PAYLOAD)])
    client.read_grid("SID", "Loader 11's nov", last_row=150, last_col="Z")
    method, url, kwargs = session.calls[0]
    assert method == "GET" and url.endswith("/spreadsheets/SID")
    assert kwargs["params"]["ranges"] == "'Loader 11''s nov'!A1:Z150"
    assert "formattedValue" in kwargs["params"]["fields"] and "merges" in kwargs["params"]["fields"]


def test_write_uses_raw_numbers_and_exact_cells():
    client, session = client_with([FakeResponse(200, {})])
    client.write_cells("SID", "Loader 11 nov 2026", [CellWrite(9, 4, 21), CellWrite(9, 5, 696.38), CellWrite(9, 6, "")])
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url.endswith("/spreadsheets/SID/values:batchUpdate")
    body = kwargs["json"]
    assert body["valueInputOption"] == "RAW"
    assert body["data"] == [
        {"range": "'Loader 11 nov 2026'!D9", "values": [[21]]},
        {"range": "'Loader 11 nov 2026'!E9", "values": [[696.38]]},
        {"range": "'Loader 11 nov 2026'!F9", "values": [[""]]},
    ]
    assert a1("A", 1, 27) == "'A'!AA1"


def test_transient_errors_are_retried_with_backoff():
    client, session = client_with([FakeResponse(503), FakeResponse(429), FakeResponse(200, {"properties": {"title": "T"}, "sheets": []})])
    assert client.get_spreadsheet_info("SID").title == "T"
    assert len(session.calls) == 3


def test_retries_give_up_after_max_attempts():
    client, session = client_with([FakeResponse(500)] * 4)
    with pytest.raises(SheetsError) as exc:
        client.get_spreadsheet_info("SID")
    assert exc.value.retryable and len(session.calls) == 4


@pytest.mark.parametrize(
    "status, message, code",
    [
        (403, "The caller does not have permission", "SHEET_ACCESS_DENIED"),
        (404, "Requested entity was not found.", "SPREADSHEET_NOT_FOUND"),
        (400, "Unable to parse range: 'X'!A1:Z150", "WORKSHEET_NOT_FOUND"),
        (400, "Invalid value", "SHEETS_REQUEST_FAILED"),
    ],
)
def test_permanent_errors_are_not_retried(status, message, code):
    client, session = client_with([FakeResponse(status, {"error": {"message": message}})])
    with pytest.raises(SheetsError) as exc:
        client.read_grid("SID", "X", last_row=10, last_col="Z")
    assert exc.value.code == code and not exc.value.retryable
    assert len(session.calls) == 1
    if code == "SHEET_ACCESS_DENIED":
        assert "bot@proj.iam.gserviceaccount.com" in exc.value.message


def test_telegram_client_honours_retry_after_and_formats_messages():
    calls = []
    responses = [
        httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 77}}),
    ]

    def handler(request: httpx.Request):
        calls.append(request)
        return responses.pop(0)

    sleeps = []
    api = TelegramBotApi("123:ABC", transport=httpx.MockTransport(handler), sleep=sleeps.append)
    result = api.send_message(5, "x" * 5000, reply_markup={"inline_keyboard": []}, reply_to_message_id=9)
    assert result == {"message_id": 77}
    assert sleeps == [3.0]
    body = json.loads(calls[-1].content)
    assert calls[-1].url.path == "/bot123:ABC/sendMessage"
    assert body["parse_mode"] == "HTML" and len(body["text"]) <= 4096
    assert body["reply_parameters"]["message_id"] == 9


def test_telegram_client_raises_on_permanent_error():
    api = TelegramBotApi(
        "123:ABC",
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked"})),
        sleep=lambda _s: None,
    )
    with pytest.raises(TelegramApiError) as exc:
        api.send_message(5, "hi")
    assert exc.value.status == 403 and not exc.value.retryable
