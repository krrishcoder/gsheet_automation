"""In-memory view of a block of worksheet cells (as returned by spreadsheets.get with grid data)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CellValue:
    effective: float | str | bool | None = None  # effectiveValue (computed value)
    formatted: str | None = None  # formattedValue (what the user sees, e.g. "696.380 MT")
    formula: str | None = None  # userEnteredValue.formulaValue, e.g. "=E9/D9"
    error: str | None = None  # effectiveValue.errorValue message, e.g. "#DIV/0!"

    @property
    def is_empty(self) -> bool:
        if self.formula or self.error:
            return False
        if self.effective is None:
            return not (self.formatted or "").strip()
        return isinstance(self.effective, str) and not self.effective.strip()

    @property
    def text(self) -> str:
        if self.formatted is not None:
            return self.formatted
        return "" if self.effective is None else str(self.effective)


EMPTY_CELL = CellValue()


@dataclass(frozen=True)
class MergedRange:
    """1-based inclusive bounds."""

    first_row: int
    last_row: int
    first_col: int
    last_col: int

    def contains(self, row: int, col: int) -> bool:
        return self.first_row <= row <= self.last_row and self.first_col <= col <= self.last_col


@dataclass
class SheetGrid:
    title: str
    rows: list[list[CellValue]]
    start_row: int = 1  # 1-based row number of rows[0]
    start_col: int = 1  # 1-based column number of rows[*][0]
    merges: list[MergedRange] = field(default_factory=list)
    parent: "SheetGrid | None" = field(default=None, repr=False, compare=False)

    def subgrid(self, first_row: int, last_row: int, first_col: int, last_col: int) -> "SheetGrid":
        """A view of one rectangular area (e.g. one loader's table); coordinates stay absolute."""
        rows = [[self.cell(r, c) for c in range(first_col, last_col + 1)] for r in range(first_row, last_row + 1)]
        return SheetGrid(
            title=self.title,
            rows=rows,
            start_row=first_row,
            start_col=first_col,
            merges=self.merges,
            parent=self.parent or self,
        )

    @property
    def last_row(self) -> int:
        return self.start_row + len(self.rows) - 1

    @property
    def last_col(self) -> int:
        width = max((len(r) for r in self.rows), default=0)
        return self.start_col + width - 1

    def cell(self, row: int, col: int) -> CellValue:
        r = row - self.start_row
        c = col - self.start_col
        if r < 0 or c < 0 or r >= len(self.rows) or c >= len(self.rows[r]):
            return EMPTY_CELL
        return self.rows[r][c]

    def label(self, row: int, col: int) -> str:
        """Display text, resolving merged cells to their top-left value (merged headers)."""
        own = self.cell(row, col)
        if not own.is_empty:
            return own.text
        source = self.parent or self  # a merge's top-left may lie outside a subgrid
        for merge in self.merges:
            if merge.contains(row, col):
                return source.cell(merge.first_row, merge.first_col).text
        return ""


@dataclass(frozen=True)
class CellWrite:
    row: int
    col: int
    value: int | float | str  # "" clears the cell
    date_format: str | None = None  # value is a date serial; show it with this pattern (e.g. dd-mm-yyyy)
