"""Test doubles for the two external APIs (Telegram Bot API, Google Sheets)."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.schemas.sheets import CellValue, CellWrite, MergedRange, SheetGrid
from app.services.google_sheets_service import SheetsError, SpreadsheetInfo
from app.services.sheet_layout import column_index, column_letter


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
        self.write_modes: list[bool] = []  # user_entered flag of each write call
        self.failures: dict[str, list[Exception]] = {}
        self.read_delay = 0.0
        self.reads: list[tuple[str, str]] = []
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

    def read_grid(self, spreadsheet_id: str, worksheet: str) -> SheetGrid:
        self._maybe_fail("read")
        self.reads.append((spreadsheet_id, worksheet))
        with self._lock:
            doc = self._spreadsheet(spreadsheet_id)
            if worksheet not in doc["tabs"]:
                raise SheetsError("WORKSHEET_NOT_FOUND", f"Worksheet not found ({worksheet})", status=400)
            ws: FakeWorksheet = doc["tabs"][worksheet]
            # like the API: the whole tab, trimmed to the used area
            max_col = max((c for _, c in ws.cells), default=0)
            used_rows = max((r for r, _ in ws.cells), default=0)
            rows = [[ws.cells.get((r, c), CellValue()) for c in range(1, max_col + 1)] for r in range(1, used_rows + 1)]
            grid = SheetGrid(title=worksheet, rows=rows, merges=list(ws.merges))
        if self.read_delay:
            time.sleep(self.read_delay)
        return grid

    def write_cells(self, spreadsheet_id: str, worksheet: str, writes: list[CellWrite], *, user_entered: bool = False) -> None:
        self._maybe_fail("write")
        with self._lock:
            ws: FakeWorksheet = self._spreadsheet(spreadsheet_id)["tabs"][worksheet]
            for w in writes:
                if w.value == "":
                    ws.cells.pop((w.row, w.col), None)
                elif user_entered and isinstance(w.value, str) and w.value.startswith("="):
                    ws.cells[(w.row, w.col)] = CellValue(effective=_evaluate(ws, w.value), formatted=None, formula=w.value)
                elif w.date_format:
                    shown = (SHEETS_EPOCH + timedelta(days=int(w.value))).strftime("%d-%m-%Y")
                    ws.cells[(w.row, w.col)] = CellValue(effective=w.value, formatted=shown)
                else:
                    ws.cells[(w.row, w.col)] = CellValue(effective=w.value, formatted=str(w.value))
            self.writes.append((spreadsheet_id, worksheet, list(writes)))
            self.write_modes.append(user_entered)


def _evaluate(ws: FakeWorksheet, formula: str):
    """The two formula shapes the bot writes: a sum of litres (=190+154.98) and the refill
    average (=AQ47/(AR47-AR46))."""
    body = formula[1:]
    if re.fullmatch(r"[\d.+]+", body):
        return sum(float(part) for part in body.split("+"))
    m = re.fullmatch(r"([A-Z]+\d+)/\(([A-Z]+\d+)-([A-Z]+\d+)\)", body)
    if m:
        litres, hm, prev = (float(ws.value(ref) or 0) for ref in m.groups())
        return litres / (hm - prev) if hm != prev else None
    raise AssertionError(f"fake sheet cannot evaluate {formula}")


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
    """A tab holding one loader's table at B1 (as the real per-loader tabs). Returns the sheet and {date: row}."""
    ws = FakeWorksheet(title)
    rows = add_month_table(ws, year, month, two_tier=two_tier, loader_number=loader_number, text_dates=text_dates)
    return ws, rows


def add_month_table(
    ws: FakeWorksheet,
    year: int,
    month: int,
    *,
    top: int = 1,
    left: int = 2,
    two_tier: bool = True,
    loader_number: int = 11,
    title: str | None = "default",
    text_dates: bool = False,
) -> dict[date, int]:
    """Write one loader's table (title row, header row(s), one row per day) with its Date column at
    column ``left`` and its title at row ``top``. Returns {date: row}."""

    def at(offset: int, row: int) -> str:
        return f"{column_letter(left + offset)}{row}"

    if title:
        text = f"Loader {loader_number:02d} – {date(year, month, 1):%B %Y}" if title == "default" else title
        ws.set(at(0, top), text)
        ws.merge(at(0, top), at(12, top))
    head = top + 1
    if two_tier:
        ws.set(at(0, head), "Date")
        ws.merge(at(0, head), at(0, head + 1))
        ws.set(at(1, head), "HSD Fuel (L)")
        ws.merge(at(1, head), at(1, head + 1))
        for offset, label in ((2, "1st Shift"), (5, "2nd Shift"), (8, "3rd Shift")):
            ws.set(at(offset, head), label)
            ws.merge(at(offset, head), at(offset + 2, head))
        ws.set(at(11, head), "Day Total")
        ws.merge(at(11, head), at(12, head))
        for offset, sub in zip(range(2, 13), ["Trips", "Weight (MT)", "Avg (MT)"] * 3 + ["Weight (MT)", "Trips"]):
            ws.set(at(offset, head + 1), sub)
        first_data = head + 2
    else:
        headers = [
            "Date", "HSD Fuel (L)",
            "1st Shift Trips", "1st Shift Weight (MT)", "1st Shift Avg (MT)",
            "2nd Shift Trips", "2nd Shift Weight (MT)", "2nd Shift Avg (MT)",
            "3rd Shift Trips", "3rd Shift Weight (MT)", "3rd Shift Avg (MT)",
            "Day Total Weight (MT)", "Day Total Trips",
        ]
        for offset, header in enumerate(headers):
            ws.set(at(offset, head), header)
        first_data = head + 1
    rows: dict[date, int] = {}
    d = date(year, month, 1)
    row = first_data
    while d.month == month:
        if text_dates:
            ws.set(at(0, row), d.strftime("%d-%m-%Y"))
        else:
            ws.set(at(0, row), serial(d), formatted=d.strftime("%d-%m-%Y"))
        rows[d] = row
        d += timedelta(days=1)
        row += 1
    return rows


def add_card_block(
    ws: FakeWorksheet,
    *,
    top: int,
    left: int = 4,
    loader_number: int = 11,
    year: int = 2026,
    month: int = 11,
    filled: bool = False,
) -> dict[date, int]:
    """A trip-card block like the real sheet's "Loader 11 Card Details" (title at D41, header at
    D42): its own Date column, HSD and a trips count per shift, but no per-shift weight/avg, so it
    is not a shift report. Total Trips / Est. Weight are formulas the bot never writes."""

    def at(offset: int, row: int) -> str:
        return f"{column_letter(left + offset)}{row}"

    ws.set(at(0, top), f"Loader {loader_number:02d} Card Details")
    headers = ["Date", "HSD Fuel (L)", "1st Shift Trips", "2nd Shift Trips", "3rd Shift Trips", "Total Trips", "Est. Weight (MT)"]
    for offset, header in enumerate(headers):
        ws.set(at(offset, top + 1), header)
    rows: dict[date, int] = {}
    d, row = date(year, month, 1), top + 2
    while d.month == month:
        ws.set(at(0, row), serial(d), formatted=d.strftime("%d-%m-%Y"))
        ws.set(at(5, row), 0, formula=f"={at(2, row)}+{at(3, row)}+{at(4, row)}")
        if filled:
            for offset, value in ((2, 11), (3, 33), (4, 14), (5, 58), (6, 1960)):
                ws.set(at(offset, row), value)
        rows[d] = row
        d += timedelta(days=1)
        row += 1
    return rows


def add_hm_block(ws: FakeWorksheet, *, top: int, left: int = 31, loader_number: int = 11, year: int = 2026, month: int = 11) -> dict[date, int]:
    """Like the real "Loader 11 Opening and Closing" (title AE41, header AE42): Date and an
    opening / closing hour-meter column per shift."""
    ws.set((top, left), f"Loader {loader_number:02d} Opening and Closing ")
    headers = ["Date"] + [f"{s} shift {w} HM" for s in ("1st", "2nd", "3rd") for w in ("Opening", "Closing")]
    for offset, header in enumerate(headers):
        ws.set((top + 1, left + offset), header)
    rows: dict[date, int] = {}
    d, row = date(year, month, 1), top + 2
    while d.month == month:
        ws.set((row, left), serial(d), formatted=d.strftime("%d-%m-%Y"))
        rows[d] = row
        d += timedelta(days=1)
        row += 1
    return rows


def add_refill_log(
    ws: FakeWorksheet, *, top: int, left: int = 41, loader_number: int = 11, entries: list[tuple[date, float, float]] = ()
) -> int:
    """Like the real "Loader 11 hsd avg" (title AO41): Date, (hidden column), hsd, hm, avg — one
    row per refill, avg = litres / HM run since the row above. Returns the first data row."""
    ws.set((top, left), f"Loader {loader_number:02d} hsd avg")
    for offset, header in ((0, "Date"), (2, "hsd"), (3, "hm"), (4, "avg")):
        ws.set((top + 1, left + offset), header)
    first = top + 2
    for i, (d, litres, hm) in enumerate(entries):
        row = first + i
        ws.set((row, left), serial(d), formatted=d.strftime("%m/%d/%Y"))
        ws.set((row, left + 2), litres)
        ws.set((row, left + 3), hm)
        if i:
            hsd_ref, hm_ref = f"{column_letter(left + 2)}{row}", f"{column_letter(left + 3)}{row}"
            formula = f"={hsd_ref}/({hm_ref}-{column_letter(left + 3)}{row - 1})"
            ws.set((row, left + 4), _evaluate(ws, formula), formula=formula)
    return first
