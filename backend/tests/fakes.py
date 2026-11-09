"""Test doubles for the two external APIs (Telegram Bot API, Google Sheets)."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.schemas.sheets import CellValue, CellWrite, MergedRange, SheetGrid
from app.services.google_sheets_service import SheetsError, SpreadsheetInfo
from app.services.sheet_layout import column_index


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self._next_id = 500
        self._lock = threading.Lock()

    def send_message(self, chat_id, text, *, reply_markup=None, reply_to_message_id=None):
        with self._lock:
            self._next_id += 1
            msg = {
                "message_id": self._next_id,
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "reply_to": reply_to_message_id,
            }
            self.sent.append(msg)
            return {"message_id": self._next_id, "chat": {"id": chat_id}}

    def edit_message_text(self, chat_id, message_id, text, *, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": message_id}

    def answer_callback_query(self, callback_query_id, text=None, *, show_alert=False):
        self.answers.append({"id": callback_query_id, "text": text, "show_alert": show_alert})

    # helpers
    def last(self, chat_id: int | None = None) -> dict[str, Any]:
        msgs = [m for m in self.sent if chat_id is None or m["chat_id"] == chat_id]
        return msgs[-1]

    def to(self, chat_id: int) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["chat_id"] == chat_id]

    @staticmethod
    def buttons(message: dict[str, Any]) -> list[str]:
        markup = message.get("reply_markup") or {}
        return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


@dataclass
class FakeWorksheet:
    title: str
    cells: dict[tuple[int, int], CellValue] = field(default_factory=dict)
    merges: list[MergedRange] = field(default_factory=list)

    def set(self, ref: str | tuple[int, int], value=None, *, formatted: str | None = None, formula: str | None = None) -> None:
        row, col = _ref(ref)
        if formatted is None and value is not None:
            formatted = str(value)
        self.cells[(row, col)] = CellValue(effective=value, formatted=formatted, formula=formula)

    def get(self, ref: str | tuple[int, int]) -> CellValue:
        return self.cells.get(_ref(ref), CellValue())

    def value(self, ref: str | tuple[int, int]):
        return self.get(ref).effective

    def merge(self, first: str, last: str) -> None:
        r1, c1 = _ref(first)
        r2, c2 = _ref(last)
        self.merges.append(MergedRange(r1, r2, c1, c2))


def _ref(ref: str | tuple[int, int]) -> tuple[int, int]:
    if isinstance(ref, tuple):
        return ref
    letters = "".join(ch for ch in ref if ch.isalpha())
    return int(ref[len(letters):]), column_index(letters)


class FakeSheets:
    service_account_email = "bot@test-project.iam.gserviceaccount.com"

    def __init__(self) -> None:
        self.spreadsheets: dict[str, dict[str, Any]] = {}
        self.writes: list[tuple[str, str, list[CellWrite]]] = []
        self.failures: dict[str, list[Exception]] = {}
        self.read_delay = 0.0
        self._lock = threading.Lock()

    def add_spreadsheet(self, spreadsheet_id: str, title: str = "Loader sheet") -> None:
        self.spreadsheets.setdefault(spreadsheet_id, {"title": title, "tabs": {}})

    def add_worksheet(self, spreadsheet_id: str, worksheet: FakeWorksheet) -> FakeWorksheet:
        self.add_spreadsheet(spreadsheet_id)
        self.spreadsheets[spreadsheet_id]["tabs"][worksheet.title] = worksheet
        return worksheet

    def tab(self, spreadsheet_id: str, title: str) -> FakeWorksheet:
        return self.spreadsheets[spreadsheet_id]["tabs"][title]

    def fail_next(self, operation: str, error: Exception, times: int = 1) -> None:
        self.failures.setdefault(operation, []).extend([error] * times)

    def _maybe_fail(self, operation: str) -> None:
        queue = self.failures.get(operation)
        if queue:
            raise queue.pop(0)

    def _spreadsheet(self, spreadsheet_id: str) -> dict[str, Any]:
        if spreadsheet_id not in self.spreadsheets:
            raise SheetsError("SPREADSHEET_NOT_FOUND", "Spreadsheet not found — check the configured URL", status=404)
        return self.spreadsheets[spreadsheet_id]

    def get_spreadsheet_info(self, spreadsheet_id: str) -> SpreadsheetInfo:
        self._maybe_fail("info")
        doc = self._spreadsheet(spreadsheet_id)
        return SpreadsheetInfo(title=doc["title"], worksheet_titles=list(doc["tabs"]))

    def read_grid(self, spreadsheet_id: str, worksheet: str, *, last_row: int, last_col: str) -> SheetGrid:
        self._maybe_fail("read")
        with self._lock:
            doc = self._spreadsheet(spreadsheet_id)
            if worksheet not in doc["tabs"]:
                raise SheetsError("WORKSHEET_NOT_FOUND", f"Worksheet not found ({worksheet})", status=400)
            ws: FakeWorksheet = doc["tabs"][worksheet]
            max_col = column_index(last_col)
            used_rows = max((r for r, _ in ws.cells), default=0)
            rows = [[ws.cells.get((r, c), CellValue()) for c in range(1, max_col + 1)] for r in range(1, min(used_rows, last_row) + 1)]
            grid = SheetGrid(title=worksheet, rows=rows, merges=list(ws.merges))
        if self.read_delay:
            time.sleep(self.read_delay)
        return grid

    def write_cells(self, spreadsheet_id: str, worksheet: str, writes: list[CellWrite]) -> None:
        self._maybe_fail("write")
        with self._lock:
            ws: FakeWorksheet = self._spreadsheet(spreadsheet_id)["tabs"][worksheet]
            for w in writes:
                if w.value == "":
                    ws.cells.pop((w.row, w.col), None)
                else:
                    ws.cells[(w.row, w.col)] = CellValue(effective=w.value, formatted=str(w.value))
            self.writes.append((spreadsheet_id, worksheet, list(writes)))


# --- Sheet builders matching the real layout (columns B..N) -------------------------------------

SHEETS_EPOCH = date(1899, 12, 30)


def serial(d: date) -> int:
    return (d - SHEETS_EPOCH).days


def build_month_sheet(
    title: str,
    year: int,
    month: int,
    *,
    two_tier: bool = True,
    loader_number: int = 11,
    text_dates: bool = False,
) -> tuple[FakeWorksheet, dict[date, int]]:
    """Title row, header row(s), then one row per day. Returns the sheet and {date: row}."""
    ws = FakeWorksheet(title)
    ws.set("B1", f"Loader {loader_number:02d} – {date(year, month, 1):%B %Y}")
    ws.merge("B1", "N1")
    if two_tier:
        ws.set("B2", "Date")
        ws.merge("B2", "B3")
        ws.set("C2", "HSD Fuel (L)")
        ws.merge("C2", "C3")
        for start, end, label in (("D", "F", "1st Shift"), ("G", "I", "2nd Shift"), ("J", "L", "3rd Shift")):
            ws.set(f"{start}2", label)
            ws.merge(f"{start}2", f"{end}2")
        ws.set("M2", "Day Total")
        ws.merge("M2", "N2")
        for col_letters, sub in zip("DEFGHIJKLMN", ["Trips", "Weight (MT)", "Avg (MT)"] * 3 + ["Weight (MT)", "Trips"]):
            ws.set(f"{col_letters}3", sub)
        first_data = 4
    else:
        headers = [
            "Date", "HSD Fuel (L)",
            "1st Shift Trips", "1st Shift Weight (MT)", "1st Shift Avg (MT)",
            "2nd Shift Trips", "2nd Shift Weight (MT)", "2nd Shift Avg (MT)",
            "3rd Shift Trips", "3rd Shift Weight (MT)", "3rd Shift Avg (MT)",
            "Day Total Weight (MT)", "Day Total Trips",
        ]
        for col_letters, header in zip("BCDEFGHIJKLMN", headers):
            ws.set(f"{col_letters}2", header)
        first_data = 3
    rows: dict[date, int] = {}
    d = date(year, month, 1)
    row = first_data
    while d.month == month:
        if text_dates:
            ws.set(f"B{row}", d.strftime("%d-%m-%Y"))
        else:
            ws.set(f"B{row}", serial(d), formatted=d.strftime("%d-%m-%Y"))
        rows[d] = row
        d += timedelta(days=1)
        row += 1
    return ws, rows
