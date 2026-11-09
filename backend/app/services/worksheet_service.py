"""Choosing the monthly worksheet (tab) inside a loader's spreadsheet.

1. An explicit mapping (loader, year, month) -> tab name always wins. If that tab does not exist
   the update fails; it is never redirected to another tab.
2. Without a mapping, and only when WORKSHEET_AUTO_DETECT is on, a tab is selected if exactly one
   tab title names this loader number, this month and this year (e.g. "Loader 11 nov 2026",
   "Oct loader 08 2026"). Zero or several candidates is an error. The chosen tab is shown in the
   confirmation summary before anything is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MONTH_ALIASES: dict[int, frozenset[str]] = {
    1: frozenset({"jan", "january"}),
    2: frozenset({"feb", "february"}),
    3: frozenset({"mar", "march"}),
    4: frozenset({"apr", "april"}),
    5: frozenset({"may"}),
    6: frozenset({"jun", "june"}),
    7: frozenset({"jul", "july"}),
    8: frozenset({"aug", "august"}),
    9: frozenset({"sep", "sept", "september"}),
    10: frozenset({"oct", "october"}),
    11: frozenset({"nov", "november"}),
    12: frozenset({"dec", "december"}),
}
MONTH_NAMES = {n: name.title() for n, name in {
    1: "january", 2: "february", 3: "march", 4: "april", 5: "may", 6: "june",
    7: "july", 8: "august", 9: "september", 10: "october", 11: "november", 12: "december",
}.items()}


class WorksheetResolutionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorksheetResolution:
    worksheet_name: str
    auto_detected: bool


def parse_month(token: str) -> int | None:
    token = token.strip().lower().rstrip(".")
    if token.isdigit():
        month = int(token)
        return month if 1 <= month <= 12 else None
    for month, aliases in MONTH_ALIASES.items():
        if token in aliases:
            return month
    return None


def _title_tokens(title: str) -> tuple[set[int], set[int], set[int]]:
    tokens = re.findall(r"[a-z]+|\d+", title.lower())
    numbers = {int(t) for t in tokens if t.isdigit() and len(t) <= 3}
    years = {int(t) for t in tokens if t.isdigit() and len(t) == 4}
    months = {m for t in tokens if t.isalpha() for m, aliases in MONTH_ALIASES.items() if t in aliases}
    return numbers, years, months


def title_matches(title: str, *, loader_number: int, year: int, month: int) -> bool:
    numbers, years, months = _title_tokens(title)
    return numbers == {loader_number} and years == {year} and months == {month}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


def resolve_worksheet(
    *,
    loader_number: int,
    year: int,
    month: int,
    configured_name: str | None,
    available_titles: list[str],
    auto_detect: bool,
) -> WorksheetResolution:
    period = f"{MONTH_NAMES[month]} {year}"
    if configured_name:
        if configured_name in available_titles:
            return WorksheetResolution(configured_name, auto_detected=False)
        near = [t for t in available_titles if _norm(t) == _norm(configured_name)]
        hint = f" A tab named '{near[0]}' exists — update the mapping with /set_worksheet." if near else ""
        raise WorksheetResolutionError(
            "WORKSHEET_NOT_FOUND",
            f"Configured worksheet '{configured_name}' for {period} does not exist in the spreadsheet.{hint}",
        )
    setup = f'/set_worksheet {loader_number} {year} {month} "<tab name>"'
    if not auto_detect:
        raise WorksheetResolutionError(
            "WORKSHEET_NOT_CONFIGURED", f"No worksheet configured for {period}. Admin: {setup}"
        )
    candidates = [t for t in available_titles if title_matches(t, loader_number=loader_number, year=year, month=month)]
    if len(candidates) == 1:
        return WorksheetResolution(candidates[0], auto_detected=True)
    if not candidates:
        raise WorksheetResolutionError(
            "WORKSHEET_NOT_CONFIGURED",
            f"No worksheet configured for {period} and no tab clearly named for it. Admin: {setup}",
        )
    raise WorksheetResolutionError(
        "WORKSHEET_AMBIGUOUS",
        f"Several tabs could be {period}: {', '.join(repr(c) for c in candidates)}. Admin: {setup}",
    )
