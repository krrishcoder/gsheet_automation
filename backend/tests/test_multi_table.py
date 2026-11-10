"""One monthly tab holding a separate table for each loader (stacked or side by side)."""

from datetime import date

import pytest
from sqlalchemy import select

from app.core.config import DEFAULT_COLUMN_MAP
from app.database.models import LoaderReportRecord
from app.services.sheet_layout import (
    Col,
    SheetLayoutError,
    column_index,
    column_letter,
    find_date_row,
    find_tables,
    locate_table,
)
from app.services.worksheet_service import resolve_worksheet
from tests.conftest import ADMIN_ID, REPORT_DATE, SAMPLE_REPORT
from tests.fakes import FakeSheets, FakeWorksheet, add_month_table

SHARED_ID = "combined-loaders-abcdefghijklmnop"
SHARED_URL = f"https://docs.google.com/spreadsheets/d/{SHARED_ID}/edit"
LOADERS = (1, 5, 11)
EXPECTED = {1: (42, 1340.84, 31.92), 5: (43, 1411.93, 32.84), 11: (21, 696.38, 33.16)}


def combined_tab(arrangement: str, *, untitled: tuple[int, ...] = (), title: str = "Nov 2026"):
    """Three loader tables in one tab. Returns the sheet and {loader: (left column, {date: row})}."""
    ws = FakeWorksheet(title)
    tables = {}
    for i, n in enumerate(LOADERS):
        top, left = {
            "stacked": (1 + i * 36, 2),  # one under another, gap rows between
            "side": (1, 2 + i * 14),  # next to each other, one empty column between
            "side_tight": (1, 2 + i * 13),  # next to each other, no gap
        }[arrangement]
        rows = add_month_table(ws, 2026, 11, top=top, left=left, loader_number=n, title=None if n in untitled else "default")
        tables[n] = (left, rows)
    return ws, tables


def grid_of(ws):
    sheets = FakeSheets()
    sheets.add_worksheet("x" * 25, ws)
    return sheets.read_grid("x" * 25, ws.title)


def setup(container, arrangement="stacked", **kwargs):
    container.today = lambda: date(2026, 11, 9)
    ws, tables = combined_tab(arrangement, **kwargs)
    container.sheets.add_spreadsheet(SHARED_ID, title="CHP loaders")
    container.sheets.add_worksheet(SHARED_ID, ws)
    return ws, tables


def cells(ws, left, row):
    """(trips, weight, avg) of the 1st shift in the table whose Date column is ``left``."""
    return tuple(ws.value(f"{column_letter(left + k)}{row}") for k in (2, 3, 4))


# --- Finding the tables -------------------------------------------------------------------------


@pytest.mark.parametrize("arrangement", ["stacked", "side", "side_tight"])
def test_each_table_and_its_loader_are_found(arrangement):
    ws, tables = combined_tab(arrangement)
    found = {t.loader_number: t for t in find_tables(grid_of(ws))}
    assert set(found) == set(LOADERS)
    for n, (left, rows) in tables.items():
        t = found[n]
        assert t.date_col == left and t.title == f"Loader {n:02d} – November 2026"
        assert t.first_col == left and t.last_col >= left + 12
        assert t.first_row <= min(rows.values()) and t.last_row >= max(rows.values())
    regions = sorted(found.values(), key=lambda t: (t.first_row, t.first_col))
    for a, b in zip(regions, regions[1:]):  # tables never overlap
        assert a.last_row < b.date_row or a.last_col < b.first_col


@pytest.mark.parametrize("arrangement", ["stacked", "side", "side_tight"])
def test_locate_table_matches_rows_only_inside_the_loaders_table(arrangement):
    ws, tables = combined_tab(arrangement)
    grid = grid_of(ws)
    for n, (left, rows) in tables.items():
        table, layout, region = locate_table(grid, n, DEFAULT_COLUMN_MAP)
        assert region.loader_number == n
        assert layout.col(Col.DATE) == left and layout.col(Col.DAY_TOTAL_TRIPS) == left + 12
        assert find_date_row(table, layout, REPORT_DATE) == rows[REPORT_DATE]


def test_missing_or_duplicate_titles_are_errors():
    ws, _ = combined_tab("stacked", untitled=(5,))
    with pytest.raises(SheetLayoutError) as exc:
        locate_table(grid_of(ws), 5, DEFAULT_COLUMN_MAP)
    assert exc.value.code == "TABLE_NOT_FOUND" and "'Loader 01 – November 2026'" in exc.value.message

    ws, _ = combined_tab("stacked")
    ws.set("B37", "Loader 01 – November 2026")  # Loader 05's table retitled as Loader 01 by mistake
    with pytest.raises(SheetLayoutError) as exc:
        locate_table(grid_of(ws), 1, DEFAULT_COLUMN_MAP)
    assert exc.value.code == "TABLE_AMBIGUOUS"


def test_single_table_titled_for_another_loader_is_refused():
    ws = FakeWorksheet("Loader 01 nov 2026")
    add_month_table(ws, 2026, 11, loader_number=5)  # tab copied from Loader 05, title not changed
    with pytest.raises(SheetLayoutError) as exc:
        locate_table(grid_of(ws), 1, DEFAULT_COLUMN_MAP)
    assert exc.value.code == "TABLE_FOR_OTHER_LOADER"
    untitled = FakeWorksheet("Loader 01 nov 2026")
    add_month_table(untitled, 2026, 11, loader_number=1, title=None)
    assert locate_table(grid_of(untitled), 1, DEFAULT_COLUMN_MAP)[2].loader_number is None  # untitled: still fine


def test_loader_tab_is_preferred_over_a_shared_month_tab():
    titles = ["Loader 11 nov 2026", "Nov 2026"]
    kw = dict(year=2026, month=11, configured_name=None, available_titles=titles, auto_detect=True)
    assert resolve_worksheet(loader_number=11, **kw).worksheet_name == "Loader 11 nov 2026"
    shared = resolve_worksheet(loader_number=5, **kw)
    assert (shared.worksheet_name, shared.shared) == ("Nov 2026", True)


# --- Through the bot ------------------------------------------------------------------------------


@pytest.mark.parametrize("arrangement", ["stacked", "side", "side_tight"])
def test_report_writes_each_loader_into_its_own_table(bot, container, arrangement):
    ws, tables = setup(container, arrangement)
    reply = bot.send(f"/set_sheet 1 5 11 {SHARED_URL}", user_id=ADMIN_ID)["text"]
    assert "November tab: 'Nov 2026' (auto-selected)" in reply

    reads_before = len(container.sheets.reads)
    summary = bot.send(SAMPLE_REPORT, user_id=ADMIN_ID)
    assert len(container.sheets.reads) - reads_before == 1  # one read of the tab for all three loaders
    for n, (left, rows) in tables.items():
        assert f"'Nov 2026' (tab auto-selected) · row {rows[REPORT_DATE]}" in summary["text"]

    result = bot.confirm(summary, user_id=ADMIN_ID)
    assert result["text"].startswith("✅ SUCCESS")
    for n, (left, rows) in tables.items():
        assert cells(ws, left, rows[REPORT_DATE]) == EXPECTED[n]
        assert cells(ws, left, rows[REPORT_DATE] + 1) == (None, None, None)  # next day untouched
    # every written cell lies inside the table of the loader it was written for
    with container.db.session() as s:
        for record in s.scalars(select(LoaderReportRecord)):
            left, rows = tables[record.loader_number]
            for ref in record.updated_values["cells"]:
                col = "".join(ch for ch in ref if ch.isalpha())
                assert left <= column_index(col) <= left + 12, (record.loader_number, ref)
                assert int(ref[len(col):]) == rows[REPORT_DATE]


def test_untitled_table_in_a_shared_tab_is_not_written(bot, container):
    ws, tables = setup(container, "stacked", untitled=(5,))
    bot.send(f"/set_sheet 1 5 11 {SHARED_URL}", user_id=ADMIN_ID)
    summary = bot.send(SAMPLE_REPORT, user_id=ADMIN_ID)
    assert "none titled 'Loader 05'" in summary["text"]
    result = bot.confirm(summary, user_id=ADMIN_ID)
    assert result["text"].startswith("⚠️ PARTIAL SUCCESS")
    left5, rows5 = tables[5]
    assert cells(ws, left5, rows5[REPORT_DATE]) == (None, None, None)
    left1, rows1 = tables[1]
    assert cells(ws, left1, rows1[REPORT_DATE]) == EXPECTED[1]


def test_shared_month_tab_with_one_untitled_table_is_never_guessed(bot, container):
    container.today = lambda: date(2026, 11, 9)
    ws = FakeWorksheet("Nov 2026")
    rows = add_month_table(ws, 2026, 11, title=None)
    container.sheets.add_spreadsheet(SHARED_ID)
    container.sheets.add_worksheet(SHARED_ID, ws)
    bot.send(f"/set_sheet 1 5 11 {SHARED_URL}", user_id=ADMIN_ID)
    summary = bot.send(SAMPLE_REPORT, user_id=ADMIN_ID)
    assert summary["text"].count("has no table titled") == 3
    assert container.sheets.writes == []
    assert cells(ws, 2, rows[REPORT_DATE]) == (None, None, None)


def test_check_sheet_shows_the_loaders_table(bot, container):
    setup(container, "side")
    bot.send(f"/set_sheet 1 5 11 {SHARED_URL}", user_id=ADMIN_ID)
    text = bot.send("/check_sheet 11 2026 11", user_id=ADMIN_ID)["text"]
    assert "Worksheet: 'Nov 2026' (auto-detected — month tab shared by loaders)" in text
    assert "Table: 'Loader 11 – November 2026'" in text and "(1 of 3 tables in this tab)" in text
    assert "SHIFT_1_TRIPS=AF" in text and "DAY_TOTAL_TRIPS=AP" in text
    assert "Date rows: 30" in text
