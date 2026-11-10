"""Parser for typed trip-card / HSD messages.

Shape (every shift line and the HSD line are optional; send what you have)::

    CARD L11
    Date: 06-11-2026
    1st: 36
    2nd: 37
    3rd: 44
    HSD: 190 + 154.98

or on one line: ``CARD L11 06-11 3rd 44 HSD 154.98``. A message is a card message when its first
line contains the word "card". As with reports, nothing is guessed: an unreadable line is an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.services.report_parser import _build_date, normalize_text, parse_date_value, parse_decimal, parse_int

CARD_FORMAT = (
    "CARD L11\n"
    "Date: 06-11-2026\n"
    "1st: 36\n"
    "2nd: 37\n"
    "3rd: 44\n"
    "HSD: 190 + 154.98"
)

_CARD_WORD_RE = re.compile(r"(?<![a-z])card(?![a-z])", re.I)
_LOADER_RE = re.compile(r"(?<![a-z0-9])(?:loader|ldr|l)[ \t]*(?:no\.?)?[ \t]*[-:#.]*[ \t]*(?P<num>\d{1,3})(?![\d./\-])", re.I)
_DATE_LABEL_RE = re.compile(r"(?<![a-z])date(?![a-z])[ \t:=\->.]*(?P<value>[^\n]*)", re.I)
# Without a year only "-" or "/" separate day and month, so an HSD amount such as 99.06 is never a date.
_SHORT_DATE_RE = re.compile(r"(?<![\d./\-])(?P<d>\d{1,2})[ \t]*[/\-][ \t]*(?P<m>\d{1,2})(?![\d/\-])")
_SHIFT_RE = re.compile(
    r"(?<![a-z0-9])(?:(?P<ord>1st|2nd|3rd|first|second|third)(?:[ \t]*shift)?|shift[ \t]*(?:no\.?)?[ \t]*(?P<num>[1-3]))"
    r"(?![a-z0-9])(?:[ \t]*trips?)?[ \t]*[:=\-.>]*[ \t]*(?P<val>\d[\d.,]*|nil+|null|none|na)?",
    re.I,
)
_SHIFT_TOKENS = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3}
_HSD_RE = re.compile(r"(?<![a-z])(?:hsd|diesel|fuel)(?![a-z])(?:[ \t]*fuel)?(?P<rest>[^\n]*)", re.I)
_HSD_SPLIT_RE = re.compile(r"\+|&|(?<![a-z])and(?![a-z])|,[ \t]+|[ \t]+", re.I)
_UNIT_RE = re.compile(r"(?i)(?:ltrs?|litres?|liters?|l)\.?$")
_NIL_RE = re.compile(r"^(?:nil+|null|none|na)$", re.I)


class CardParseError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class ParsedCard:
    loader_number: int
    entry_date: date
    trips: dict[int, int] = field(default_factory=dict)  # shift -> trips, only the shifts that were sent
    hsd_fills: list[Decimal] = field(default_factory=list)  # tanker fills for the day, in litres


def is_card_message(text: str) -> bool:
    normalized = normalize_text(text or "")
    first_line = normalized.split("\n", 1)[0]
    return bool(_CARD_WORD_RE.search(first_line))


def _header(text: str) -> str:
    """The first line without its shift/HSD part (one-line messages), where loader and date live."""
    first = text.split("\n", 1)[0]
    cut = [m.start() for m in (_SHIFT_RE.search(first), _HSD_RE.search(first)) if m]
    return first[: min(cut)] if cut else first


def _parse_date(text: str, header: str, today: date, errors: list[str]) -> date | None:
    labelled = _DATE_LABEL_RE.search(text)
    candidates = [labelled.group("value")] if labelled else [header]
    for candidate in candidates:
        try:
            full = parse_date_value(candidate)
        except ValueError:
            errors.append(f"Date: '{candidate.strip()}' is not a valid date")
            return None
        if full:
            return full
        short = _SHORT_DATE_RE.search(candidate)
        if short:
            day, month = int(short["d"]), int(short["m"])
            try:
                found = _build_date(today.year, month, day)
            except ValueError:
                errors.append(f"Date: '{short.group(0)}' is not a valid date")
                return None
            # "06-11" typed in early January means last year's November
            return found if found <= today else _build_date(today.year - 1, month, day)
    if labelled:
        errors.append(f"Date: could not read '{labelled.group('value').strip()}' (use 06-11-2026)")
    else:
        errors.append("Date missing — add a line like 'Date: 06-11-2026'")
    return None


def _parse_loader(text: str, header: str, errors: list[str]) -> int | None:
    match = _LOADER_RE.search(header)
    if match is None:
        line = next((l for l in text.split("\n") if re.match(r"(?i)\s*(?:loader|ldr)\b", l)), "")
        match = _LOADER_RE.search(line)
    if match is None:
        errors.append("Loader missing — start with 'CARD L11' (L and the loader number)")
        return None
    return int(match["num"])


def _parse_shifts(text: str, errors: list[str]) -> dict[int, int]:
    trips: dict[int, int] = {}
    for match in _SHIFT_RE.finditer(text):
        shift = _SHIFT_TOKENS[match["ord"].lower()] if match["ord"] else int(match["num"])
        label = {1: "1st", 2: "2nd", 3: "3rd"}[shift]
        value = match["val"]
        if value is None:
            errors.append(f"{label} shift: no trips number after it (e.g. '{label}: 36')")
            continue
        if _NIL_RE.match(value):
            continue  # Nil: nothing to save for that shift
        try:
            number = parse_int(value)
        except ValueError as exc:
            errors.append(f"{label} shift: {exc}")
            continue
        if shift in trips and trips[shift] != number:
            errors.append(f"{label} shift given twice ({trips[shift]} and {number})")
            continue
        trips[shift] = number
    return trips


def _parse_hsd(text: str, errors: list[str]) -> list[Decimal]:
    fills: list[Decimal] = []
    for match in _HSD_RE.finditer(text):
        rest = match["rest"]
        # stop at a shift written after HSD on the same line ("HSD 154.98 3rd 44")
        shift = _SHIFT_RE.search(rest)
        rest = re.sub(r"\([^)]*\)", " ", rest[: shift.start()] if shift else rest).strip(" \t:=-.>")  # "HSD (L): 190"
        parts = [p for p in (_UNIT_RE.sub("", p.strip()) for p in _HSD_SPLIT_RE.split(rest)) if p]
        if not parts:
            errors.append("HSD: no litres after it (e.g. 'HSD: 190 + 154.98')")
            continue
        for part in parts:
            if _NIL_RE.match(part):
                continue
            try:
                value = parse_decimal(part)
            except ValueError as exc:
                errors.append(f"HSD: {exc}")
                continue
            if value <= 0:
                errors.append(f"HSD: '{part}' must be more than 0")
                continue
            fills.append(value)
    return fills


def parse_card(raw_text: str, *, today: date) -> ParsedCard:
    text = normalize_text(raw_text or "")
    errors: list[str] = []
    header = _header(text)
    loader = _parse_loader(text, header, errors)
    entry_date = _parse_date(text, header, today, errors)
    body = text[len(header):]  # the loader/date header must not be read as a shift or HSD value
    trips = _parse_shifts(body, errors)
    hsd = _parse_hsd(body, errors)
    if not errors and not trips and not hsd:
        errors.append("Nothing to save — add a shift line ('1st: 36') or an HSD line ('HSD: 190')")
    if entry_date and entry_date > today:
        errors.append(f"Date {entry_date:%d-%m-%Y} is in the future")
    if errors:
        raise CardParseError(errors)
    assert loader is not None and entry_date is not None
    return ParsedCard(loader_number=loader, entry_date=entry_date, trips=trips, hsd_fills=hsd)
