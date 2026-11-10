from datetime import date
from decimal import Decimal

import pytest

from app.core.config import DEFAULT_COLUMN_MAP
from app.schemas.sheets import SheetGrid
from app.services.calculations import ShiftValues, compute_row_update
from app.services.sheet_layout import (
    Col,
    SheetLayoutError,
    build_cell_writes,
    column_index,
    column_letter,
    detect_layout,
    find_date_row,
    read_row_state,
)
from tests.fakes import FakeSheets, FakeWorksheet, build_month_sheet


def grid_of(ws: FakeWorksheet) -> SheetGrid:
    sheets = FakeSheets()
    sheets.add_worksheet("x" * 25, ws)
    return sheets.read_grid("x" * 25, ws.title)


EXPECTED = {Col(k): column_index(v) for k, v in DEFAULT_COLUMN_MAP.items()}


@pytest.mark.parametrize("two_tier", [True, False])
def test_header_detection_matches_real_layout(two_tier):
    ws, rows = build_month_sheet("Loader 11 nov 2026", 2026, 11, two_tier=two_tier)
    layout = detect_layout(grid_of(ws), DEFAULT_COLUMN_MAP)
    assert layout.columns == EXPECTED
    assert layout.detected and not layout.deviations
    assert layout.data_start_row == rows[date(2026, 11, 1)]
    assert layout.header_rows == ([2, 3] if two_tier else [2])


def test_header_not_on_row_one_and_unmerged_group_labels():
    ws = FakeWorksheet("t")
    ws.set("B1", "CHP production")
    ws.set("B5", "Loader 11")  # title rows above
    ws.set("D6", "1st Shift")  # group labels without merges
    ws.set("G6", "2nd Shift")
    ws.set("J6", "3rd Shift")
    ws.set("M6", "Day Total")
    ws.set("B7", "Date")
    ws.set("C7", "HSD Fuel (L)")
    for col, label in zip("DEFGHIJKLMN", ["Trips", "Weight (MT)", "Avg (MT)"] * 3 + ["Weight (MT)", "Trips"]):
        ws.set(f"{col}7", label)
    ws.set("B8", 46332, formatted="06-11-2026")
    layout = detect_layout(grid_of(ws), DEFAULT_COLUMN_MAP)
    assert layout.columns == EXPECTED
    assert layout.header_rows == [6, 7]
    assert find_date_row(grid_of(ws), layout, date(2026, 11, 6)) == 8


def test_detection_follows_moved_columns():
    ws, _ = build_month_sheet("t", 2026, 11, two_tier=False)
    # Insert an extra column at C by shifting everything right of B.
    moved = FakeWorksheet("t")
    for (row, col), cell in ws.cells.items():
        moved.cells[(row, col + 1 if col >= 3 else col)] = cell
    moved.set("C2", "Remarks")
    layout = detect_layout(grid_of(moved), DEFAULT_COLUMN_MAP)
    assert layout.letter(Col.SHIFT_1_TRIPS) == "E"
    assert layout.letter(Col.DAY_TOTAL_TRIPS) == "O"
    assert any("SHIFT_1_TRIPS: expected D, found E" in d for d in layout.deviations)


def test_unrecognised_headers_fail_strict_mode():
    ws = FakeWorksheet("t")
    ws.set("B2", "Date")
    ws.set("C2", "Something else")
    with pytest.raises(SheetLayoutError) as exc:
        detect_layout(grid_of(ws), DEFAULT_COLUMN_MAP)
    assert exc.value.code == "HEADER_NOT_RECOGNISED"


def test_non_strict_mode_falls_back_to_configured_columns():
    ws = FakeWorksheet("t")
    ws.set("B2", "Date")
    ws.set("B3", 46332)
    layout = detect_layout(grid_of(ws), DEFAULT_COLUMN_MAP, strict=False)
    assert layout.columns[Col.SHIFT_2_WEIGHT] == column_index("H")


def test_missing_date_header():
    ws = FakeWorksheet("t")
    ws.set("B2", "Trips")
    with pytest.raises(SheetLayoutError) as exc:
        detect_layout(grid_of(ws), DEFAULT_COLUMN_MAP)
    assert exc.value.code == "HEADER_NOT_FOUND"


def test_detection_disabled_uses_configured_mapping():
    ws, rows = build_month_sheet("t", 2026, 11)
    layout = detect_layout(grid_of(ws), DEFAULT_COLUMN_MAP, detection=False)
    assert layout.columns == EXPECTED
    assert layout.data_start_row == rows[date(2026, 11, 1)]


@pytest.mark.parametrize("text_dates", [False, True])
def test_date_row_matching_serial_and_display_strings(text_dates):
    ws, rows = build_month_sheet("t", 2026, 11, text_dates=text_dates)
    grid = grid_of(ws)
    layout = detect_layout(grid, DEFAULT_COLUMN_MAP)
    assert find_date_row(grid, layout, date(2026, 11, 6)) == rows[date(2026, 11, 6)]
    assert find_date_row(grid, layout, date(2026, 11, 30)) == rows[date(2026, 11, 30)]


def test_date_serial_wins_over_us_style_display():
    ws, rows = build_month_sheet("t", 2026, 11)
    # A US-formatted display must not make 6 Nov look like 11 Jun.
    ws.set(f"B{rows[date(2026, 11, 6)]}", 46332, formatted="11/06/2026")
    grid = grid_of(ws)
    layout = detect_layout(grid, DEFAULT_COLUMN_MAP)
    assert find_date_row(grid, layout, date(2026, 11, 6)) == rows[date(2026, 11, 6)]


def test_date_not_found_and_duplicates():
    ws, rows = build_month_sheet("t", 2026, 11)
    grid = grid_of(ws)
    layout = detect_layout(grid, DEFAULT_COLUMN_MAP)
    with pytest.raises(SheetLayoutError) as exc:
        find_date_row(grid, layout, date(2026, 12, 1))
    assert exc.value.code == "DATE_NOT_FOUND"
    ws.set(f"B{rows[date(2026, 11, 7)]}", "06-11-2026")
    grid = grid_of(ws)
    with pytest.raises(SheetLayoutError) as exc:
        find_date_row(grid, layout, date(2026, 11, 6))
    assert exc.value.code == "DUPLICATE_DATE_ROWS"


def test_read_row_state_and_writes_touch_only_target_cells():
    ws, rows = build_month_sheet("t", 2026, 11)
    r = rows[date(2026, 11, 6)]
    ws.set(f"C{r}", 120.5, formatted="120.50 L")
    ws.set(f"G{r}", 10)
    ws.set(f"H{r}", 300.5, formatted="300.500 MT")
    ws.set(f"I{r}", "=H{0}/G{0}".format(r), formula=f"=H{r}/G{r}")
    grid = grid_of(ws)
    layout = detect_layout(grid, DEFAULT_COLUMN_MAP)
    state = read_row_state(grid, layout, r)
    assert state.shifts[2] == ShiftValues(10, Decimal("300.5"))
    assert state.hsd_fuel == Decimal("120.5")
    assert Col.SHIFT_2_AVG in state.formulas

    comp = compute_row_update(state.shifts, 1, 21, Decimal("696.380"))
    writes = build_cell_writes(layout, r, comp, reported_trips=21, reported_weight=Decimal("696.380"), formulas=state.formulas)
    cells = {f"{column_letter(w.col)}{w.row}": w.value for w in writes}
    assert cells == {f"D{r}": 21, f"E{r}": 696.38, f"F{r}": 33.16, f"M{r}": 996.88, f"N{r}": 31}


def test_formula_total_cells_are_preserved():
    ws, rows = build_month_sheet("t", 2026, 11)
    r = rows[date(2026, 11, 6)]
    ws.set(f"M{r}", 0, formula=f"=E{r}+H{r}+K{r}")
    ws.set(f"N{r}", 0, formula=f"=D{r}+G{r}+J{r}")
    grid = grid_of(ws)
    layout = detect_layout(grid, DEFAULT_COLUMN_MAP)
    state = read_row_state(grid, layout, r)
    comp = compute_row_update(state.shifts, 1, 21, Decimal("696.380"))
    writes = build_cell_writes(layout, r, comp, reported_trips=21, reported_weight=Decimal("696.380"), formulas=state.formulas)
    assert {column_letter(w.col) for w in writes} == {"D", "E", "F"}


def test_text_with_units_in_input_cells_is_read_and_garbage_rejected():
    from app.services.sheet_layout import CellValueError

    ws, rows = build_month_sheet("t", 2026, 11)
    r = rows[date(2026, 11, 6)]
    ws.set(f"H{r}", "300.500 MT")
    grid = grid_of(ws)
    layout = detect_layout(grid, DEFAULT_COLUMN_MAP)
    assert read_row_state(grid, layout, r).shifts[2].weight == Decimal("300.500")
    ws.set(f"H{r}", "about 300")
    with pytest.raises(CellValueError):
        read_row_state(grid_of(ws), layout, r)


def test_column_letters_roundtrip():
    for idx in (1, 2, 14, 26, 27, 52, 703):
        assert column_index(column_letter(idx)) == idx


@pytest.mark.parametrize("header", ["Quantity", "Quantity (MT)", "Weight (MT)", "Total Quantity"])
def test_day_total_weight_headed_just_quantity(header):
    ws, rows = build_month_sheet("t", 2026, 11, two_tier=False)
    ws.set("M2", header)  # was "Day Total Weight (MT)"
    layout = detect_layout(grid_of(ws), DEFAULT_COLUMN_MAP)
    assert layout.letter(Col.DAY_TOTAL_WEIGHT) == "M" and layout.letter(Col.SHIFT_1_WEIGHT) == "E"
