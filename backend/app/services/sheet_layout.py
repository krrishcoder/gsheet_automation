"""Worksheet layout: header detection, date-row matching, reading and writing a day's row.

Header detection works on the header *text*, not on fixed positions, and supports both
single-row headers ("1st Shift Trips") and two-tier headers where a merged "1st Shift" group
cell sits above "Trips | Weight (MT) | Avg (MT)" sub-headers. The header row does not need to
be row 1 (a merged title row usually sits above it).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum

from app.schemas.sheets import CellValue, CellWrite, SheetGrid
from app.services.calculations import RowComputation, ShiftValues
from app.services.report_parser import parse_date_value
from app.services.worksheet_service import loader_number_in_title


class Col(str, Enum):
    DATE = "DATE"
    HSD_FUEL = "HSD_FUEL"
    SHIFT_1_TRIPS = "SHIFT_1_TRIPS"
    SHIFT_1_WEIGHT = "SHIFT_1_WEIGHT"
    SHIFT_1_AVG = "SHIFT_1_AVG"
    SHIFT_2_TRIPS = "SHIFT_2_TRIPS"
    SHIFT_2_WEIGHT = "SHIFT_2_WEIGHT"
    SHIFT_2_AVG = "SHIFT_2_AVG"
    SHIFT_3_TRIPS = "SHIFT_3_TRIPS"
    SHIFT_3_WEIGHT = "SHIFT_3_WEIGHT"
    SHIFT_3_AVG = "SHIFT_3_AVG"
    DAY_TOTAL_WEIGHT = "DAY_TOTAL_WEIGHT"
    DAY_TOTAL_TRIPS = "DAY_TOTAL_TRIPS"


REQUIRED_COLUMNS = tuple(c for c in Col if c is not Col.HSD_FUEL)


def shift_columns(shift: int) -> tuple[Col, Col, Col]:
    return Col(f"SHIFT_{shift}_TRIPS"), Col(f"SHIFT_{shift}_WEIGHT"), Col(f"SHIFT_{shift}_AVG")


def column_index(letters: str) -> int:
    index = 0
    for ch in letters.strip().upper():
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index


def column_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


class SheetLayoutError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SheetLayout:
    columns: dict[Col, int]
    header_rows: list[int]
    data_start_row: int
    detected: bool
    deviations: list[str] = field(default_factory=list)  # detected vs configured differences

    def col(self, logical: Col) -> int:
        return self.columns[logical]

    def letter(self, logical: Col) -> str:
        return column_letter(self.columns[logical])


# --- Header classification ----------------------------------------------------------------------

_SHIFT_HEADER_PATTERNS = {
    1: re.compile(r"\b(?:1st|first|ist|1|i)\s*shift\b|\bshift\s*(?:1|i|one)\b|\b(?:1st|first)\b"),
    2: re.compile(r"\b(?:2nd|second|iind|2|ii)\s*shift\b|\bshift\s*(?:2|ii|two)\b|\b(?:2nd|second)\b"),
    3: re.compile(r"\b(?:3rd|third|iiird|3|iii)\s*shift\b|\bshift\s*(?:3|iii|three)\b|\b(?:3rd|third)\b"),
}
_TRIP_RE = re.compile(r"\btrips?\b")
_AVG_RE = re.compile(r"\b(?:avg|average)\b")
_WEIGHT_RE = re.compile(r"\b(?:weight|wt|qty|quantity|tonnage|tonnes?|tons?|mt)\b")
_TOTAL_RE = re.compile(r"\btotal\b")
_FUEL_RE = re.compile(r"\b(?:hsd|fuel|diesel)\b")
_DATE_RE = re.compile(r"\bdates?\b")
_METRIC_RE = re.compile(r"\b(?:trips?|avg|average|weight|wt|qty|quantity|mt)\b")


def normalize_label(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _shift_of(label: str) -> int | None:
    hits = [n for n, pattern in _SHIFT_HEADER_PATTERNS.items() if pattern.search(label)]
    return hits[0] if len(hits) == 1 else None


def classify_label(label: str) -> Col | None:
    """Map a (combined, normalised) header label to a logical column."""
    if not label:
        return None
    shift = _shift_of(label)
    has_avg = bool(_AVG_RE.search(label))
    has_trip = bool(_TRIP_RE.search(label))
    has_weight = bool(_WEIGHT_RE.search(label))
    if shift:
        if has_avg:
            return Col(f"SHIFT_{shift}_AVG")
        if has_trip:
            return Col(f"SHIFT_{shift}_TRIPS")
        if has_weight:
            return Col(f"SHIFT_{shift}_WEIGHT")
        return None
    if _TOTAL_RE.search(label) and not has_avg:
        if has_trip:
            return Col.DAY_TOTAL_TRIPS
        if has_weight:
            return Col.DAY_TOTAL_WEIGHT
        return None
    if _FUEL_RE.search(label):
        return Col.HSD_FUEL
    if _DATE_RE.search(label) and not (has_trip or has_weight or has_avg):
        return Col.DATE
    return None


def _is_group_label(label: str) -> bool:
    """A group header such as "1st Shift" or "Day Total" (no metric word of its own)."""
    if not label:
        return False
    return (_shift_of(label) is not None or bool(_TOTAL_RE.search(label))) and not _METRIC_RE.search(label)


_DATE_HEADER_WORDS = frozenset({"date", "dates", "date day", "day date"})


def _find_date_header(grid: SheetGrid, scan_rows: int, date_col: int | None) -> tuple[int, int]:
    last = min(grid.last_row, grid.start_row + scan_rows - 1)
    for row in range(grid.start_row, last + 1):
        cols = [date_col] if date_col else range(grid.start_col, grid.last_col + 1)
        for col in cols:
            if normalize_label(grid.label(row, col)) in _DATE_HEADER_WORDS:
                return row, col
    raise SheetLayoutError(
        "HEADER_NOT_FOUND", f"No 'Date' header cell found in the first {scan_rows} rows of '{grid.title}'"
    )


def _row_has_shift_groups(grid: SheetGrid, row: int) -> bool:
    return any(_is_group_label(normalize_label(grid.label(row, c))) for c in range(grid.start_col, grid.last_col + 1))


def _row_is_subheader(grid: SheetGrid, row: int, date_col: int) -> bool:
    if cell_to_date(grid.cell(row, date_col)) is not None:
        return False
    for col in range(grid.start_col, grid.last_col + 1):
        value = grid.cell(row, col).effective
        # Only literal text counts: a number formatted as "696.380 MT" is data, not a header.
        if isinstance(value, str) and _METRIC_RE.search(normalize_label(value)):
            return True
    return False


def _combined_labels(grid: SheetGrid, rows: list[int]) -> dict[int, str]:
    per_col: dict[int, list[str]] = {c: [] for c in range(grid.start_col, grid.last_col + 1)}
    for i, row in enumerate(rows):
        is_last = i == len(rows) - 1
        carry = ""
        carry_left = 0
        for col in range(grid.start_col, grid.last_col + 1):
            text = normalize_label(grid.label(row, col))
            if text:
                carry, carry_left = (text, 2) if (not is_last and _is_group_label(text)) else ("", 0)
            elif carry and carry_left > 0:
                # Unmerged group header spanning blank cells to its right ("1st Shift", "", "").
                text = carry
                carry_left -= 1
            if text and (not per_col[col] or per_col[col][-1] != text):
                per_col[col].append(text)
    return {col: " ".join(parts) for col, parts in per_col.items()}


def detect_layout(
    grid: SheetGrid,
    configured: dict[str, str],
    *,
    detection: bool = True,
    strict: bool = True,
    scan_rows: int = 20,
) -> SheetLayout:
    configured_cols = {Col(k): column_index(v) for k, v in configured.items()}
    if not detection:
        date_row, _ = _find_date_header(grid, scan_rows, configured_cols[Col.DATE])
        header_rows = [date_row]
        if _row_is_subheader(grid, date_row + 1, configured_cols[Col.DATE]):
            header_rows.append(date_row + 1)
        return SheetLayout(
            columns=configured_cols, header_rows=header_rows, data_start_row=header_rows[-1] + 1, detected=False
        )

    date_row, date_col = _find_date_header(grid, scan_rows, None)
    header_rows = [date_row]
    if date_row - 1 >= grid.start_row and _row_has_shift_groups(grid, date_row - 1):
        header_rows.insert(0, date_row - 1)
    if _row_is_subheader(grid, date_row + 1, date_col):
        header_rows.append(date_row + 1)

    labels = _combined_labels(grid, header_rows)
    found: dict[Col, list[int]] = {}
    for col, label in labels.items():
        logical = classify_label(label)
        if logical:
            found.setdefault(logical, []).append(col)
    found.setdefault(Col.DATE, [date_col])

    columns: dict[Col, int] = {}
    missing: list[str] = []
    ambiguous: list[str] = []
    for logical in REQUIRED_COLUMNS:
        cols = found.get(logical, [])
        if len(cols) == 1:
            columns[logical] = cols[0]
        elif len(cols) > 1:
            ambiguous.append(f"{logical.value} ({', '.join(column_letter(c) for c in cols)})")
        else:
            missing.append(logical.value)

    if strict and (missing or ambiguous):
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if ambiguous:
            details.append("ambiguous: " + "; ".join(ambiguous))
        raise SheetLayoutError(
            "HEADER_NOT_RECOGNISED", f"Could not recognise the column headers in '{grid.title}' ({'; '.join(details)})"
        )
    for logical in REQUIRED_COLUMNS:
        columns.setdefault(logical, configured_cols[logical])

    if len(set(columns.values())) != len(columns):
        raise SheetLayoutError("HEADER_NOT_RECOGNISED", f"Two fields map to the same column in '{grid.title}'")

    # HSD fuel is never written; it is only located so it can be shown and left untouched.
    fuel_cols = found.get(Col.HSD_FUEL, [])
    fuel_col = fuel_cols[0] if len(fuel_cols) == 1 else configured_cols[Col.HSD_FUEL]
    if fuel_col not in columns.values():
        columns[Col.HSD_FUEL] = fuel_col

    deviations = [
        f"{logical.value}: expected {column_letter(configured_cols[logical])}, found {column_letter(col)}"
        for logical, col in columns.items()
        if configured_cols[logical] != col
    ]
    return SheetLayout(
        columns=columns,
        header_rows=header_rows,
        data_start_row=header_rows[-1] + 1,
        detected=True,
        deviations=deviations,
    )


# --- Several tables in one tab -------------------------------------------------------------------

_TITLE_ROWS_ABOVE = 4  # title + group-header rows that may sit above a table's "Date" cell
_SAME_BAND_ROWS = 3  # "Date" cells this close vertically belong to side-by-side tables


@dataclass(frozen=True)
class TableRegion:
    """One table in a tab; bounds are absolute and inclusive."""

    date_row: int
    date_col: int
    first_row: int
    last_row: int
    first_col: int
    last_col: int
    loader_number: int | None  # from a title such as "Loader 05 – November 2026"
    title: str | None

    def describe(self) -> str:
        where = f"rows {self.first_row}–{self.last_row}, columns {column_letter(self.first_col)}–{column_letter(self.last_col)}"
        return f"'{self.title}' ({where})" if self.title else where


def find_tables(grid: SheetGrid) -> list[TableRegion]:
    """Every table in the tab, found by its "Date" header cell.

    A table reaches to the column before the next table on its right (side by side) and to the
    row before the next table below it (stacked). Its loader is the one named by a "Loader NN"
    title in the few rows above its header, within its own columns.
    """
    anchors: list[tuple[int, int]] = []
    for row in range(grid.start_row, grid.last_row + 1):
        for col in range(grid.start_col, grid.last_col + 1):
            if normalize_label(grid.cell(row, col).text) not in _DATE_HEADER_WORDS:
                continue
            # a two-tier header may repeat "Date" in the row below: same table
            if not any(c == col and 0 < row - r <= _SAME_BAND_ROWS for r, c in anchors):
                anchors.append((row, col))

    tables: list[TableRegion] = []
    for row, col in anchors:
        right = [c for r, c in anchors if c > col and abs(r - row) <= _SAME_BAND_ROWS]
        last_col = min(right) - 1 if right else grid.last_col
        below = [r for r, c in anchors if r > row + _SAME_BAND_ROWS and col <= c <= last_col]
        last_row = min(below) - 1 if below else grid.last_row
        first_row = max(grid.start_row, row - _TITLE_ROWS_ABOVE)

        numbers: dict[int, str] = {}
        for r in range(first_row, row):
            for c in range(col, last_col + 1):
                text = grid.label(r, c)
                number = loader_number_in_title(text) if text else None
                if number is not None:
                    numbers.setdefault(number, text.strip())
        loader, title = (next(iter(numbers.items())) if len(numbers) == 1 else (None, None))
        tables.append(TableRegion(row, col, first_row, last_row, col, last_col, loader, title))
    return tables


def _shifted_config(table: TableRegion, configured: dict[str, str]) -> dict[str, str]:
    """Configured letters describe a table whose Date column is the configured DATE column;
    shift them so the fallback mapping also fits tables placed further right."""
    offset = table.date_col - column_index(configured["DATE"])
    shifted = {key: column_letter(column_index(letter) + offset) for key, letter in configured.items()}
    if min(column_index(letter) for letter in shifted.values()) < 1:
        return dict(configured)
    return shifted


def _region(grid: SheetGrid, table: TableRegion) -> SheetGrid:
    return grid.subgrid(table.first_row, table.last_row, table.first_col, table.last_col)


def find_report_tables(
    grid: SheetGrid, configured: dict[str, str], *, scan_rows: int = 20
) -> tuple[list[TableRegion], list[TableRegion]]:
    """(shift-report tables, ignored tables).

    A summary block with its own "Date" column (e.g. "Loader 11 Card Details": Date, Fuel, trips
    per shift) is not a shift report and must neither be written nor make a loader ambiguous.
    A table counts when its headers name every shift's Trips/Weight/Avg and the day totals. If no
    table qualifies, all of them are returned, so the usual header errors are still reported.
    """
    tables = find_tables(grid)
    report, ignored = [], []
    for table in tables:
        (report if _is_report_table(grid, table, configured, scan_rows) else ignored).append(table)
    return (report, ignored) if report else (tables, [])


def _is_report_table(grid: SheetGrid, table: TableRegion, configured: dict[str, str], scan_rows: int) -> bool:
    try:
        detect_layout(_region(grid, table), _shifted_config(table, configured), scan_rows=scan_rows)
    except SheetLayoutError:
        return False
    return True


def locate_table(
    grid: SheetGrid,
    loader_number: int,
    configured: dict[str, str],
    *,
    detection: bool = True,
    strict: bool = True,
    scan_rows: int = 20,
    require_title: bool = False,
) -> tuple[SheetGrid, SheetLayout, TableRegion | None]:
    """The part of the tab that belongs to this loader, with its column layout.

    * One table in the tab: the whole tab is the loader's (as before), unless its title names a
      different loader, or ``require_title`` is set (a month tab shared by all loaders) and the
      title does not name this loader.
    * Several tables: exactly one must be titled for this loader. Callers must use the returned
      subgrid for row matching, so another loader's rows can never be matched or written.

    Only shift-report tables count (see ``find_report_tables``); summary blocks are ignored.
    """
    tables, ignored = find_report_tables(grid, configured, scan_rows=scan_rows)
    unrecognised = "".join(
        f". {t.describe()} is titled for it, but its headers are not a shift report (1st/2nd/3rd Shift Trips, Weight, Avg, Day Total)"
        for t in ignored
        if t.loader_number == loader_number
    )
    if len(tables) <= 1:
        table = tables[0] if tables else None
        if table and table.loader_number not in (None, loader_number):
            raise SheetLayoutError(
                "TABLE_FOR_OTHER_LOADER",
                f"The table in '{grid.title}' is titled for Loader {table.loader_number:02d}, not Loader {loader_number:02d}",
            )
        if require_title and (table is None or table.loader_number != loader_number):
            raise SheetLayoutError(
                "TABLE_NOT_FOUND",
                f"'{grid.title}' has no table titled 'Loader {loader_number:02d}'. "
                f"Put a title such as 'Loader {loader_number:02d} – <month>' above its header row{unrecognised}",
            )
        if not ignored:
            layout = detect_layout(grid, configured, detection=detection, strict=strict, scan_rows=scan_rows)
            return grid, layout, table
        # summary blocks share the tab: work only inside the report table
    else:
        matches = [t for t in tables if t.loader_number == loader_number]
        if len(matches) != 1:
            titles = ", ".join(f"'{t.title}'" for t in tables if t.title) or "none"
            if matches:
                raise SheetLayoutError(
                    "TABLE_AMBIGUOUS",
                    f"'{grid.title}' has {len(matches)} tables titled for Loader {loader_number:02d}: "
                    + "; ".join(t.describe() for t in matches),
                )
            raise SheetLayoutError(
                "TABLE_NOT_FOUND",
                f"'{grid.title}' has {len(tables)} tables but none titled 'Loader {loader_number:02d}' "
                f"(titles found: {titles}). Put a title such as 'Loader {loader_number:02d} – <month>' above its header row{unrecognised}",
            )
        table = matches[0]
    sub = _region(grid, table)
    layout = detect_layout(sub, _shifted_config(table, configured), detection=detection, strict=strict, scan_rows=scan_rows)
    return sub, layout, table


# --- A loader's other blocks: card details, opening/closing HM, HSD refill log -----------------

_OPEN_RE = re.compile(r"\bopen")
_CLOSE_RE = re.compile(r"\bclos")
_HM_RE = re.compile(r"\b(?:hm|hmr|hour ?meter)\b")
_SPEED_RE = re.compile(r"\bspeed\b")


def _card_key(label: str) -> str | None:
    logical = classify_label(label)
    if logical is Col.HSD_FUEL:
        return "hsd"
    for shift in (1, 2, 3):
        if logical is Col(f"SHIFT_{shift}_TRIPS"):
            return f"trips{shift}"
    return None


def _hm_key(label: str) -> str | None:
    shift = _shift_of(label)
    if shift and _OPEN_RE.search(label):
        return f"open{shift}"
    if shift and _CLOSE_RE.search(label):
        return f"close{shift}"
    return None


def _speed_key(label: str) -> str | None:
    shift = _shift_of(label)
    return f"speed{shift}" if shift and _SPEED_RE.search(label) else None


def _daily_key(label: str) -> str | None:
    """A loader kept as one row per day (e.g. "Loader 8": Date, HSD, HM, Total TRIP, Per trip avg,
    AVG). Per-shift columns mark a different kind of block."""
    if _shift_of(label):
        return "per_shift" if _TRIP_RE.search(label) or _WEIGHT_RE.search(label) else None
    has_trip, has_avg = bool(_TRIP_RE.search(label)), bool(_AVG_RE.search(label))
    if has_trip and has_avg:
        return "trip_avg"  # litres per trip
    if has_avg:
        return "avg"  # litres per HM hour since the previous refill
    if has_trip:
        return "trips"
    if _HM_RE.search(label):
        return "hm"
    if _FUEL_RE.search(label):
        return "hsd"
    if _WEIGHT_RE.search(label):
        return "qty"
    return None


def _log_key(label: str) -> str | None:
    if _shift_of(label):
        return None
    if _AVG_RE.search(label):
        return "avg"
    if _HM_RE.search(label):
        return "hm"
    if _FUEL_RE.search(label):
        return "hsd"
    return None


@dataclass(frozen=True)
class BlockKind:
    name: str
    classify: object  # label -> column key
    required: tuple[str, ...]
    optional: tuple[str, ...]
    example: str  # how to title and head such a block ({n} = loader number)
    forbidden: tuple[str, ...] = ()  # a column with this key means a different kind of block


BLOCK_KINDS = {
    "card": BlockKind(
        "card block",
        _card_key,
        ("trips1", "trips2", "trips3"),
        ("hsd",),
        "'Loader {n} Card Details' with the columns Date, HSD Fuel (L), 1st Shift Trips, 2nd Shift Trips, 3rd Shift Trips",
    ),
    "hm": BlockKind(
        "opening/closing HM block",
        _hm_key,
        ("open1", "close1", "open2", "close2", "open3", "close3"),
        (),
        "'Loader {n} Opening and Closing' with the columns Date, 1st shift Opening HM, 1st shift Closing HM, "
        "… 3rd shift Closing HM",
    ),
    "log": BlockKind(
        "HSD refill log",
        _log_key,
        ("hsd", "hm"),
        ("avg",),
        "'Loader {n} hsd avg' with the columns Date, hsd, hm, avg",
    ),
    "daily": BlockKind(
        "daily table",
        _daily_key,
        ("hsd", "trips"),
        ("hm", "avg", "qty", "trip_avg"),
        "'Loader {n}' with the columns Date, HSD, HM, Total TRIP, AVG",
        forbidden=("per_shift",),
    ),
    "speed": BlockKind(
        "speed block",
        _speed_key,
        ("speed1", "speed2", "speed3"),
        (),
        "'Loader {n} Per trip Speed' with the columns Date, 1st shift speed, 2nd shift speed, 3rd shift speed",
    ),
}


@dataclass(frozen=True)
class Block:
    """One of a loader's blocks, e.g. "Loader 11 Card Details". Columns are keyed by name
    (trips1, hsd, open2, close3, hm, avg, …); ``grid`` holds only the block's own cells."""

    kind: str
    grid: SheetGrid
    layout: SheetLayout  # its Date column and first data row
    columns: dict[str, int]
    region: TableRegion
    bounded: bool  # another table starts right below it (an append-only log cannot grow past it)

    def ref(self, key: str, row: int) -> str:
        return f"{column_letter(self.columns[key])}{row}"


def detect_block_layout(grid: SheetGrid, kind: str, *, scan_rows: int = 20) -> tuple[SheetLayout, dict[str, int]]:
    spec = BLOCK_KINDS[kind]
    date_row, date_col = _find_date_header(grid, scan_rows, None)
    header_rows = [date_row]
    if date_row - 1 >= grid.start_row and _row_has_shift_groups(grid, date_row - 1):
        header_rows.insert(0, date_row - 1)
    if _row_is_subheader(grid, date_row + 1, date_col):
        header_rows.append(date_row + 1)
    found: dict[str, list[int]] = {}
    for col, label in _combined_labels(grid, header_rows).items():
        key = spec.classify(label) if col != date_col else None
        if key:
            found.setdefault(key, []).append(col)
    columns: dict[str, int] = {}
    problems = []
    if any(key in found for key in spec.forbidden):
        raise SheetLayoutError("BLOCK_OTHER_KIND", f"not a {spec.name} (it has per-shift columns)")
    for key in spec.required + spec.optional:
        cols = found.get(key, [])
        if len(cols) == 1:
            columns[key] = cols[0]
        elif cols:
            problems.append(f"'{key}' in more than one column ({', '.join(column_letter(c) for c in cols)})")
        elif key in spec.required:
            problems.append(f"no '{key}' column")
    if problems:
        # no column of this kind at all: a different kind of block, not a mistyped one
        code = "BLOCK_HEADER_NOT_RECOGNISED" if found else "BLOCK_OTHER_KIND"
        raise SheetLayoutError(code, f"{spec.name} headers not recognised ({'; '.join(problems)})")
    layout = SheetLayout(columns={Col.DATE: date_col}, header_rows=header_rows, data_start_row=header_rows[-1] + 1, detected=True)
    return layout, columns


def locate_block(
    grid: SheetGrid, loader_number: int, kind: str, configured: dict[str, str], *, scan_rows: int = 20
) -> Block:
    """The loader's block of this kind: a table titled for the loader (e.g. "Loader 11 hsd avg")
    that is not its shift report and whose headers fit the kind."""
    spec = BLOCK_KINDS[kind]
    found, reasons = [], []
    for table in find_tables(grid):
        if table.loader_number != loader_number or _is_report_table(grid, table, configured, scan_rows):
            continue
        sub = _region(grid, table)
        try:
            layout, columns = detect_block_layout(sub, kind, scan_rows=scan_rows)
        except SheetLayoutError as exc:
            if exc.code == "BLOCK_HEADER_NOT_RECOGNISED":
                reasons.append(f"{table.describe()}: {exc.message}")
            continue
        columns = {"date": layout.col(Col.DATE), **columns}
        found.append(Block(kind, sub, layout, columns, table, bounded=table.last_row < grid.last_row))
    if len(found) == 1:
        return found[0]
    n = f"{loader_number:02d}"
    if found:
        raise SheetLayoutError(
            "BLOCK_AMBIGUOUS",
            f"'{grid.title}' has {len(found)} {spec.name}s for Loader {n}: " + "; ".join(b.region.describe() for b in found),
        )
    raise SheetLayoutError(
        "BLOCK_NOT_FOUND",
        f"'{grid.title}' has no {spec.name} for Loader {n}. Add a block titled {spec.example.format(n=n)}"
        + (f" ({'; '.join(reasons)})" if reasons else ""),
    )


# --- Cell value conversion ------------------------------------------------------------------

_SHEETS_EPOCH = date(1899, 12, 30)
_UNIT_RE = re.compile(r"(?i)\s*(mt|m\.t\.?|l|ltr|litres?|liters?|trips?|tons?|tonnes?)\.?\s*$")


class CellValueError(ValueError):
    pass


def cell_to_date(cell: CellValue) -> date | None:
    """Real date cells arrive as serial numbers (unambiguous); text cells are read day-first."""
    value = cell.effective
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 <= value <= 80000:
            return _SHEETS_EPOCH + timedelta(days=int(value))
        return None
    text = value if isinstance(value, str) else cell.formatted
    if not text or not text.strip():
        return None
    try:
        return parse_date_value(text)
    except ValueError:
        return None


def cell_to_decimal(cell: CellValue, *, what: str) -> Decimal | None:
    if cell.error:
        raise CellValueError(f"{what} contains an error value ({cell.error})")
    value = cell.effective
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise CellValueError(f"{what} contains TRUE/FALSE")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = _UNIT_RE.sub("", value.strip()).replace(",", "").strip()
    if text in {"", "-", "--"} or text.lower() in {"nil", "na", "n/a"}:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise CellValueError(f"{what} contains non-numeric text '{value}'") from exc


def cell_to_int(cell: CellValue, *, what: str) -> int | None:
    value = cell_to_decimal(cell, what=what)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise CellValueError(f"{what} is not a whole number ({value})")
    return int(value)


# --- Rows -----------------------------------------------------------------------------------------


def find_date_row(grid: SheetGrid, layout: SheetLayout, target: date) -> int:
    date_col = layout.col(Col.DATE)
    matches = [
        row for row in range(layout.data_start_row, grid.last_row + 1) if cell_to_date(grid.cell(row, date_col)) == target
    ]
    if not matches:
        raise SheetLayoutError(
            "DATE_NOT_FOUND", f"No row for {target:%d-%m-%Y} in column {layout.letter(Col.DATE)} of '{grid.title}'"
        )
    if len(matches) > 1:
        raise SheetLayoutError(
            "DUPLICATE_DATE_ROWS",
            f"{target:%d-%m-%Y} appears in more than one row of '{grid.title}' (rows {', '.join(map(str, matches))})",
        )
    return matches[0]


def date_rows_summary(grid: SheetGrid, layout: SheetLayout) -> list[tuple[int, date]]:
    date_col = layout.col(Col.DATE)
    rows = []
    for row in range(layout.data_start_row, grid.last_row + 1):
        d = cell_to_date(grid.cell(row, date_col))
        if d:
            rows.append((row, d))
    return rows


@dataclass(frozen=True)
class RowState:
    shifts: dict[int, ShiftValues]
    averages: dict[int, Decimal | None]
    day_total_weight: Decimal | None
    day_total_trips: Decimal | None
    hsd_fuel: Decimal | None
    formulas: frozenset[Col]

    def snapshot(self) -> dict:
        def s(v):
            return None if v is None else str(v)

        return {
            "shifts": {str(n): v.to_dict() for n, v in self.shifts.items()},
            "averages": {str(n): s(v) for n, v in self.averages.items()},
            "day_total_weight": s(self.day_total_weight),
            "day_total_trips": s(self.day_total_trips),
            "hsd_fuel": s(self.hsd_fuel),
        }


def read_row_state(grid: SheetGrid, layout: SheetLayout, row: int, *, target_shift: int | None = None) -> RowState:
    """Read the three shifts of a day row.

    Shift input cells must be numbers or blank. Derived cells (averages/totals) are read leniently:
    they are recomputed anyway, so text or errors there do not block an update.
    """
    shifts: dict[int, ShiftValues] = {}
    averages: dict[int, Decimal | None] = {}
    for n in (1, 2, 3):
        trips_col, weight_col, avg_col = shift_columns(n)
        shifts[n] = ShiftValues(
            trips=cell_to_int(grid.cell(row, layout.col(trips_col)), what=f"{layout.letter(trips_col)}{row}"),
            weight=cell_to_decimal(grid.cell(row, layout.col(weight_col)), what=f"{layout.letter(weight_col)}{row}"),
        )
        averages[n] = _lenient_decimal(grid.cell(row, layout.col(avg_col)))
    formulas = frozenset(c for c in Col if c in layout.columns and grid.cell(row, layout.col(c)).formula)
    return RowState(
        shifts=shifts,
        averages=averages,
        day_total_weight=_lenient_decimal(grid.cell(row, layout.col(Col.DAY_TOTAL_WEIGHT))),
        day_total_trips=_lenient_decimal(grid.cell(row, layout.col(Col.DAY_TOTAL_TRIPS))),
        hsd_fuel=(
            _lenient_decimal(grid.cell(row, layout.col(Col.HSD_FUEL))) if Col.HSD_FUEL in layout.columns else None
        ),
        formulas=formulas,
    )


def _lenient_decimal(cell: CellValue) -> Decimal | None:
    try:
        return cell_to_decimal(cell, what="cell")
    except CellValueError:
        return None


def build_cell_writes(
    layout: SheetLayout,
    row: int,
    computation: RowComputation,
    *,
    reported_trips: int | None,
    reported_weight: Decimal | None,
    formulas: frozenset[Col],
) -> list[CellWrite]:
    """Cells to write for one shift update. Never touches HSD fuel, other shifts or formula cells."""
    trips_col, weight_col, avg_col = shift_columns(computation.shift)
    writes: list[CellWrite] = []
    if reported_trips is not None:
        writes.append(CellWrite(row, layout.col(trips_col), computation.after.trips))
    if reported_weight is not None:
        writes.append(CellWrite(row, layout.col(weight_col), _num(computation.after.weight)))
    derived = (
        (avg_col, computation.average),
        (Col.DAY_TOTAL_WEIGHT, computation.day_total_weight),
        (Col.DAY_TOTAL_TRIPS, computation.day_total_trips),
    )
    for logical, value in derived:
        if logical in formulas:
            continue  # the sheet computes this itself; keep the formula
        writes.append(CellWrite(row, layout.col(logical), _num(value)))
    return writes


def _num(value: Decimal | int | None) -> int | float | str:
    if value is None:
        return ""
    if isinstance(value, int):
        return value
    return float(value)
