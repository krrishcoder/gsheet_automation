"""Month summary of a loader for the boss, read from the sheet right after an update.

Everything comes from the loader's month tab, as the sheet computes it:

* trips and quantity: the month's shift values in the loader's shift table
* diesel avg: the latest refill's avg in the refill log ("Loader 11 hsd avg"), in L per HM hour
* speed: the latest shift's speed in the speed block ("Loader 11 Per trip Speed")
* net operating profit / profit with GST: the values next to the labels "Operating Profit
  (Pre-GST)" and "Profit With Gst" inside the loader's blocks

A figure the tab does not have (e.g. no refill log for a loader) is left out. The text is plain
WhatsApp text (``*bold*``); Telegram shows it with a wa.me button that opens WhatsApp with it typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from urllib.parse import quote

from app.core.config import Settings
from app.database.database import Database
from app.database.repositories import LoaderRepository
from app.schemas.sheets import CellValue, SheetGrid
from app.services.google_sheets_service import SheetsClient, SheetsError
from app.services.sheet_layout import (
    CellValueError,
    Col,
    SheetLayoutError,
    cell_to_date,
    date_rows_summary,
    find_tables,
    locate_block,
    locate_table,
    normalize_label,
    read_row_state,
)
from app.services.worksheet_service import MONTH_NAMES, WorksheetResolutionError, resolve_worksheet

_OPERATING_PROFIT_RE = re.compile(r"(?:net )?operating profit(?: (?:pre|before) gst)?")
_PROFIT_GST_RE = re.compile(r"(?:net )?profit with gst")
_SHIFT = {1: "1st", 2: "2nd", 3: "3rd"}


@dataclass
class LoaderSummary:
    loader_number: int
    year: int
    month: int
    till: date | None = None  # last day with shift data
    trips: int = 0
    quantity: Decimal = Decimal(0)
    diesel_avg: str | None = None  # as the sheet shows it, e.g. "16.146"
    diesel_hm: str | None = None  # HM of that refill
    speed: str | None = None
    speed_at: tuple[date, int] | None = None  # (day, shift)
    operating_profit: str | None = None
    profit_gst: str | None = None
    problems: list[str] = field(default_factory=list)


def _positive(cell: CellValue) -> bool:
    value = cell.effective
    return not cell.error and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _shown(cell: CellValue) -> str:
    return (cell.formatted or str(cell.effective)).strip()


def _three_decimals(cell: CellValue) -> str:
    """16.14634146 -> "16.146", 8.4 -> "8.4" (as the sheet's 0.000 format, without padding)."""
    return format(Decimal(str(cell.effective)).quantize(Decimal("0.001")).normalize(), "f")


class SummaryService:
    def __init__(self, db: Database, sheets: SheetsClient, settings: Settings) -> None:
        self.db = db
        self.sheets = sheets
        self.settings = settings

    def build(self, loader_numbers: list[int], year: int, month: int) -> list[LoaderSummary]:
        """One summary per loader; a loader whose tab cannot be read gets its reason in ``problems``."""
        with self.db.session() as session:
            loaders = LoaderRepository(session)
            targets = []
            for n in loader_numbers:
                loader = loaders.get_by_number(n)
                if loader is None or not loader.is_active:
                    continue
                mapping = loaders.get_worksheet(loader.id, year, month)
                targets.append((n, loader.spreadsheet_id, mapping.worksheet_name if mapping and mapping.is_active else None))
        grids: dict[tuple[str, str], SheetGrid] = {}  # one read per tab
        summaries = []
        for n, spreadsheet_id, configured in targets:
            summary = LoaderSummary(n, year, month)
            try:
                info = self.sheets.get_spreadsheet_info(spreadsheet_id)
                resolution = resolve_worksheet(
                    loader_number=n,
                    year=year,
                    month=month,
                    configured_name=configured,
                    available_titles=info.worksheet_titles,
                    auto_detect=self.settings.worksheet_auto_detect,
                )
                key = (spreadsheet_id, resolution.worksheet_name)
                if key not in grids:
                    grids[key] = self.sheets.read_grid(*key)
                self._fill(summary, grids[key], resolution.shared)
            except SheetsError as exc:
                if exc.retryable:
                    raise
                summary.problems.append(exc.message)
            except (WorksheetResolutionError, SheetLayoutError) as exc:
                summary.problems.append(exc.message)
            summaries.append(summary)
        return summaries

    def _fill(self, s: LoaderSummary, grid: SheetGrid, shared: bool) -> None:
        cfg = self.settings
        table, layout, _ = locate_table(
            grid,
            s.loader_number,
            cfg.sheet_column_map,
            detection=cfg.sheet_header_detection,
            strict=cfg.sheet_header_strict,
            scan_rows=cfg.sheet_header_scan_rows,
            require_title=shared,
        )
        for row, day in date_rows_summary(table, layout):
            if (day.year, day.month) != (s.year, s.month):
                continue
            try:
                state = read_row_state(table, layout, row)
            except CellValueError as exc:
                s.problems.append(f"{day:%d-%m} skipped: {exc}")
                continue
            values = [v for v in state.shifts.values() if not v.is_empty]
            if values:
                s.till = day
                s.trips += sum(v.trips or 0 for v in values)
                s.quantity += sum((v.weight or Decimal(0) for v in values), Decimal(0))
        self._diesel(s, grid)
        self._speed(s, grid)
        s.operating_profit = self._labelled(grid, s.loader_number, _OPERATING_PROFIT_RE)
        s.profit_gst = self._labelled(grid, s.loader_number, _PROFIT_GST_RE)

    def _block(self, grid: SheetGrid, n: int, kind: str):
        try:
            return locate_block(grid, n, kind, self.settings.sheet_column_map, scan_rows=self.settings.sheet_header_scan_rows)
        except SheetLayoutError:
            return None

    def _diesel(self, s: LoaderSummary, grid: SheetGrid) -> None:
        """The latest refill's avg (the bottom row of the log with an avg)."""
        log = self._block(grid, s.loader_number, "log")
        if log is None or "avg" not in log.columns:
            return
        for row in range(log.grid.last_row, log.layout.data_start_row - 1, -1):
            avg = log.grid.cell(row, log.columns["avg"])
            if _positive(avg):
                s.diesel_avg = _three_decimals(avg)
                hm = log.grid.cell(row, log.columns["hm"])
                s.diesel_hm = None if hm.is_empty else _shown(hm)
                return

    def _speed(self, s: LoaderSummary, grid: SheetGrid) -> None:
        """The latest shift with a speed (empty shifts compute 0 or #DIV/0! and are skipped)."""
        block = self._block(grid, s.loader_number, "speed")
        if block is None:
            return
        for row in range(block.grid.last_row, block.layout.data_start_row - 1, -1):
            day = cell_to_date(block.grid.cell(row, block.layout.col(Col.DATE)))
            if day is None or (day.year, day.month) != (s.year, s.month):
                continue
            for shift in (3, 2, 1):
                cell = block.grid.cell(row, block.columns[f"speed{shift}"])
                if _positive(cell):
                    s.speed, s.speed_at = _three_decimals(cell), (day, shift)
                    return

    @staticmethod
    def _labelled(grid: SheetGrid, n: int, pattern: re.Pattern[str]) -> str | None:
        """The first number to the right of a label such as "Operating Profit (Pre-GST)", looked up
        only inside blocks titled for this loader."""
        for region in find_tables(grid):
            if region.loader_number != n:
                continue
            for row in range(region.first_row, region.last_row + 1):
                for col in range(region.first_col, region.last_col + 1):
                    if not pattern.fullmatch(normalize_label(grid.cell(row, col).text)):
                        continue
                    for right in range(col + 1, region.last_col + 1):
                        cell = grid.cell(row, right)
                        if not cell.error and isinstance(cell.effective, (int, float)) and not isinstance(cell.effective, bool):
                            return _shown(cell)
        return None


# --- Text -----------------------------------------------------------------------------------------


def render_summary(summaries: list[LoaderSummary], *, note: str | None = None) -> str:
    """Plain WhatsApp text (``*bold*``), one paragraph per loader."""
    if not summaries:
        return ""
    first = summaries[0]
    lines = [f"*Loader summary · {MONTH_NAMES[first.month]} {first.year}*"]
    if note:
        lines.append(note)
    for s in summaries:
        lines.append("")
        till = f" (till {s.till:%d-%m})" if s.till else ""
        lines.append(f"*Loader {s.loader_number:02d}*{till}")
        lines.append(f"Trips: {s.trips:,}")
        lines.append(f"Quantity: {s.quantity:,.2f} MT")
        if s.diesel_avg:
            at = f" (last refill, HM {s.diesel_hm})" if s.diesel_hm else " (last refill)"
            lines.append(f"Diesel avg: {s.diesel_avg} L/h{at}")
        if s.speed and s.speed_at:
            day, shift = s.speed_at
            lines.append(f"Speed: {s.speed} min/trip ({day:%d-%m}, {_SHIFT[shift]} shift)")
        if s.operating_profit:
            lines.append(f"Net operating profit: {s.operating_profit}")
        if s.profit_gst:
            lines.append(f"Profit with GST: {s.profit_gst}")
        if s.problems and not s.till:
            lines.append("(could not read: " + "; ".join(s.problems[:2]) + ")")
    return "\n".join(lines)


def normalize_phone(text: str) -> str | None:
    """WhatsApp number as wa.me wants it: country code + number, digits only. A 10-digit Indian
    mobile number gets 91 in front."""
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits
    return digits if 11 <= len(digits) <= 15 else None


def whatsapp_link(text: str, phone: str | None = None) -> str:
    """Click-to-chat link: opens WhatsApp with ``text`` typed (to ``phone``, or a chat you pick)."""
    return f"https://wa.me/{phone or ''}?text={quote(text, safe='')}"
