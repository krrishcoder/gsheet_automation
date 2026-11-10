"""Google Sheets API v4 client (service-account auth, REST over google-auth's AuthorizedSession).

Only three operations are needed:
* list the tabs of a spreadsheet,
* read a block of cells with effective values, formatted values, formulas and merges,
* write individual cells with valueInputOption=RAW (numbers stay numbers; existing number
  formats such as ``0.000 "MT"`` are cell properties and are not touched).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from app.schemas.sheets import CellValue, CellWrite, MergedRange, SheetGrid
from app.services.retry import call_with_retries
from app.services.sheet_layout import column_letter

log = logging.getLogger(__name__)

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
API_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class SheetsError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class SpreadsheetInfo:
    title: str
    worksheet_titles: list[str]


class SheetsClient(Protocol):
    service_account_email: str | None

    def get_spreadsheet_info(self, spreadsheet_id: str) -> SpreadsheetInfo: ...

    def read_grid(self, spreadsheet_id: str, worksheet: str) -> SheetGrid: ...

    def write_cells(self, spreadsheet_id: str, worksheet: str, writes: list[CellWrite]) -> None: ...


def quote_sheet_name(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def a1(worksheet: str, row: int, col: int) -> str:
    return f"{quote_sheet_name(worksheet)}!{column_letter(col)}{row}"


def parse_grid_response(payload: dict[str, Any], fallback_title: str) -> SheetGrid:
    sheets = payload.get("sheets") or []
    if not sheets:
        return SheetGrid(title=fallback_title, rows=[])
    sheet = sheets[0]
    title = sheet.get("properties", {}).get("title", fallback_title)
    data = (sheet.get("data") or [{}])[0]
    rows: list[list[CellValue]] = []
    for row_data in data.get("rowData", []) or []:
        cells = []
        for value in row_data.get("values", []) or []:
            effective = value.get("effectiveValue") or {}
            error = effective.get("errorValue")
            if "numberValue" in effective:
                eff: Any = effective["numberValue"]
            elif "stringValue" in effective:
                eff = effective["stringValue"]
            elif "boolValue" in effective:
                eff = effective["boolValue"]
            else:
                eff = None
            cells.append(
                CellValue(
                    effective=eff,
                    formatted=value.get("formattedValue"),
                    formula=(value.get("userEnteredValue") or {}).get("formulaValue"),
                    error=(error.get("message") or error.get("type")) if error else None,
                )
            )
        rows.append(cells)
    merges = [
        MergedRange(
            first_row=m.get("startRowIndex", 0) + 1,
            last_row=m.get("endRowIndex", 0),
            first_col=m.get("startColumnIndex", 0) + 1,
            last_col=m.get("endColumnIndex", 0),
        )
        for m in sheet.get("merges", []) or []
    ]
    return SheetGrid(
        title=title,
        rows=rows,
        start_row=data.get("startRow", 0) + 1,
        start_col=data.get("startColumn", 0) + 1,
        merges=merges,
    )


class GoogleSheetsClient:
    def __init__(
        self,
        credentials: Any,
        *,
        timeout: float = 30.0,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        session_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._session_factory = session_factory or self._default_session
        self._local = threading.local()
        self.service_account_email: str | None = getattr(credentials, "service_account_email", None)

    @classmethod
    def from_settings(cls, settings) -> "GoogleSheetsClient":
        from google.oauth2 import service_account

        if settings.google_service_account_json:
            info = json.loads(settings.google_service_account_json)
            credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            credentials = service_account.Credentials.from_service_account_file(
                settings.google_service_account_file, scopes=SCOPES
            )
        return cls(credentials, timeout=settings.google_api_timeout_seconds, max_attempts=settings.google_api_max_attempts)

    @staticmethod
    def _default_session(credentials: Any) -> Any:
        from google.auth.transport.requests import AuthorizedSession

        return AuthorizedSession(credentials)

    def _session(self) -> Any:
        # requests sessions and credential refresh are not thread-safe: one session per thread.
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._session_factory(self._credentials)
            self._local.session = session
        return session

    # --- HTTP plumbing ------------------------------------------------------------------------

    def _request(self, method: str, url: str, *, description: str, **kwargs: Any) -> dict[str, Any]:
        def attempt() -> dict[str, Any]:
            from google.auth import exceptions as auth_exceptions

            try:
                response = self._session().request(method, url, timeout=self._timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                raise SheetsError("SHEETS_UNAVAILABLE", f"Network error reaching Google Sheets: {exc}", retryable=True) from exc
            except auth_exceptions.RefreshError as exc:
                raise SheetsError("SHEETS_AUTH_FAILED", f"Service account authentication failed: {exc}") from exc
            except auth_exceptions.TransportError as exc:
                raise SheetsError("SHEETS_UNAVAILABLE", f"Network error during authentication: {exc}", retryable=True) from exc
            if response.status_code >= 400:
                raise self._error_from_response(response)
            return response.json() if response.content else {}

        return call_with_retries(
            attempt,
            is_retryable=lambda exc: isinstance(exc, SheetsError) and exc.retryable,
            max_attempts=self._max_attempts,
            sleep=self._sleep,
            description=description,
        )

    def _error_from_response(self, response: Any) -> SheetsError:
        status = response.status_code
        try:
            message = response.json().get("error", {}).get("message", "")
        except ValueError:
            message = (response.text or "")[:300]
        if status in _RETRYABLE_STATUS:
            return SheetsError("SHEETS_UNAVAILABLE", f"Google Sheets is temporarily unavailable ({status})", retryable=True, status=status)
        if status == 403:
            who = self.service_account_email or "the service account"
            return SheetsError(
                "SHEET_ACCESS_DENIED",
                f"Access denied. Share the spreadsheet with {who} as Editor. (Google: {message})",
                status=status,
            )
        if status == 404:
            return SheetsError("SPREADSHEET_NOT_FOUND", "Spreadsheet not found — check the configured URL", status=status)
        if status == 400 and "Unable to parse range" in message:
            return SheetsError("WORKSHEET_NOT_FOUND", f"Worksheet not found ({message})", status=status)
        return SheetsError("SHEETS_REQUEST_FAILED", f"Google Sheets rejected the request ({status}): {message}", status=status)

    # --- Operations -------------------------------------------------------------------------------

    def get_spreadsheet_info(self, spreadsheet_id: str) -> SpreadsheetInfo:
        payload = self._request(
            "GET",
            f"{API_BASE}/{spreadsheet_id}",
            params={"fields": "properties.title,sheets.properties(sheetId,title,index)"},
            description="spreadsheets.get(metadata)",
        )
        titles = [s.get("properties", {}).get("title", "") for s in payload.get("sheets", [])]
        return SpreadsheetInfo(title=payload.get("properties", {}).get("title", ""), worksheet_titles=titles)

    def read_grid(self, spreadsheet_id: str, worksheet: str) -> SheetGrid:
        """The whole tab. A fixed range (e.g. A1:Z150) silently cut off tables placed further right
        or lower; the API returns only the used area, so reading the whole tab stays small."""
        payload = self._request(
            "GET",
            f"{API_BASE}/{spreadsheet_id}",
            params={
                "ranges": quote_sheet_name(worksheet),
                "includeGridData": "true",
                "fields": (
                    "sheets(properties(title),merges,"
                    "data(startRow,startColumn,rowData(values(userEnteredValue,effectiveValue,formattedValue))))"
                ),
            },
            description="spreadsheets.get(grid)",
        )
        return parse_grid_response(payload, worksheet)

    def write_cells(self, spreadsheet_id: str, worksheet: str, writes: list[CellWrite]) -> None:
        if not writes:
            return
        body = {
            "valueInputOption": "RAW",
            "data": [{"range": a1(worksheet, w.row, w.col), "values": [[w.value]]} for w in writes],
        }
        self._request(
            "POST",
            f"{API_BASE}/{spreadsheet_id}/values:batchUpdate",
            json=body,
            description="values.batchUpdate",
        )
