"""Parser for typed trip-card / hour-meter / HSD messages.

Short form (every code is optional; send what you have)::

    L11 10-11
    T1 37 O1 4268 C1 4272
    T2 35 O2 4272 C2 4276.2
    T3 40 O3 4276.2 C3 4280
    H 132.4 4276.2

* first line: loader and date (``L11 10-11``)
* ``T1``-``T3`` trips, ``O1``-``O3`` opening HM, ``C1``-``C3`` closing HM of that shift
* ``H <litres> <HM>``: a diesel refill and the hour meter at the refill (one ``H`` per refill)
* ``T <trips>`` (no shift digit): the day's total trips, for a loader kept as one row per day
  (``L08 10-11 H 120 5355.2 T 110``)

The older long form still works (``CARD L11`` / ``Date: 06-11-2026`` / ``1st: 36`` /
``HSD: 190 + 154.98``), and both can be mixed. As with reports nothing is guessed: any number
the parser could not place is an error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.services.report_parser import (
    _DATE_PATTERNS,
    _build_date,
    normalize_text,
    parse_date_value,
    parse_decimal,
    parse_int,
)

CARD_FORMAT = (
    "L11 10-11\n"
    "T1 37 O1 4268 C1 4272\n"
    "T2 35 O2 4272 C2 4276.2\n"
    "T3 40 O3 4276.2 C3 4280\n"
    "H 132.4 4276.2"
)

_CARD_WORD_RE = re.compile(r"(?<![a-z])card(?![a-z])", re.I)
_LOADER_RE = re.compile(r"(?<![a-z0-9])(?:loader|ldr|l)[ \t]*(?:no\.?)?[ \t]*[-:#.]*[ \t]*(?P<num>\d{1,3})(?![\d./\-])", re.I)
# "L11 …" / "LDR 11 …" start a card message; "Loader 11" only when a date follows (so that a
# forwarded report section such as "LOADER - 01" is never taken for one)
_SHORT_HEADER_RE = re.compile(
    r"^(?:(?:ldr|l)[ \t]*[-:#.]*[ \t]*\d{1,3}(?![\d./\-])"
    r"|loader[ \t]*[-:#.]*[ \t]*\d{1,3}(?![\d./\-]).*?\d{1,2}[ \t]*[/\-.][ \t]*\d{1,2})",
    re.I,
)
_DATE_LABEL_RE = re.compile(r"(?<![a-z])date(?![a-z])[ \t:=\->.]*(?P<value>[^\n]*)", re.I)
# Without a year only "-" or "/" separate day and month, so an HSD amount such as 99.06 is never a date.
_SHORT_DATE_RE = re.compile(r"(?<![\d./\-])(?P<d>\d{1,2})[ \t]*[/\-][ \t]*(?P<m>\d{1,2})(?![\d/\-])")
_SHIFT_RE = re.compile(
    r"(?<![a-z0-9])(?:(?P<ord>1st|2nd|3rd|first|second|third)(?:[ \t]*shift)?|shift[ \t]*(?:no\.?)?[ \t]*(?P<num>[1-3]))"
    r"(?![a-z0-9])(?:[ \t]*trips?)?[ \t]*[:=\-.>]*[ \t]*(?P<val>\d[\d.,]*|nil+|null|none|na)?",
    re.I,
)
_SHIFT_TOKENS = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3}
# T1 37 · O1 4268 · C1 4276.2 (T = trips, O = opening HM, C = closing HM; the digit, written right
# after the letter, is the shift)
_CODE_RE = re.compile(r"(?<![a-z0-9])(?P<code>[toc])(?P<shift>[1-3])(?![\d.,])[ \t]*[:=\-]*[ \t]*(?P<val>\d[\d.,]*|nil+)?", re.I)
# T 110 / Total 110 / Trips: 110 — the day's total trips. A space or ":" is needed after T, so "T137"
# (a mistyped "T1 37") is reported instead of being read as 137 trips.
_TOTAL_TRIPS_RE = re.compile(
    r"(?<![a-z0-9])(?:total[ \t]*trips?|total|trips?|t)(?:[ \t]+|[ \t]*[:=\-]+[ \t]*)(?P<val>\d[\d.,]*|nil+)(?![\d])",
    re.I,
)
# H 132.4 4276.2 (litres, then the hour meter at the refill)
_REFILL_RE = re.compile(
    r"(?<![a-z0-9])h(?![a-z])[ \t]*[:=\-]*[ \t]*(?P<litres>\d[\d.,]*)?"
    r"(?:[ \t]*(?:ltrs?|litres?|liters?|l)\.?(?![a-z]))?"
    r"(?:[ \t]*(?:@|at(?![a-z])|/|hm)?[ \t]*[:=\-]*[ \t]*(?P<hm>\d[\d.,]*))?",
    re.I,
)
_HSD_RE = re.compile(r"(?<![a-z])(?:hsd|diesel|fuel)(?![a-z])(?:[ \t]*fuel)?(?P<rest>[^\n]*)", re.I)
_HSD_SPLIT_RE = re.compile(r"\+|&|(?<![a-z])and(?![a-z])|,[ \t]+", re.I)
_HSD_UNIT_RE = re.compile(r"(?<=[\d\s])(?:ltrs?|litres?|liters?|l)\.?(?![a-z])", re.I)
_NIL_RE = re.compile(r"^(?:nil+|null|none|na)$", re.I)
_TOKENS_RE = (_SHIFT_RE, _CODE_RE, _TOTAL_TRIPS_RE, _REFILL_RE, _HSD_RE)
_LABEL = {1: "1st", 2: "2nd", 3: "3rd"}


class CardParseError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Refill:
    litres: Decimal
    hm: Decimal | None  # hour meter at the refill; None for an "HSD:" line, which has no HM


@dataclass
class ParsedCard:
    loader_number: int
    entry_date: date
    trips: dict[int, int] = field(default_factory=dict)  # shift -> trips, only the shifts that were sent
    opening: dict[int, Decimal] = field(default_factory=dict)  # shift -> opening hour meter
    closing: dict[int, Decimal] = field(default_factory=dict)  # shift -> closing hour meter
    refills: list[Refill] = field(default_factory=list)
    total_trips: int | None = None  # the day's trips, for a loader without shifts

    @property
    def hsd_fills(self) -> list[Decimal]:
        return [r.litres for r in self.refills]


def is_card_message(text: str) -> bool:
    first_line = normalize_text(text or "").split("\n", 1)[0]
    return bool(_CARD_WORD_RE.search(first_line) or _SHORT_HEADER_RE.match(first_line))


class _Body:
    """The message after its header line, with the parts that were read marked off."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._read = bytearray(len(text))

    def take(self, start: int, end: int) -> None:
        self._read[start:end] = b"\x01" * (end - start)

    def is_read(self, index: int) -> bool:
        return bool(self._read[index])

    def unread_numbers(self) -> list[str]:
        rest = "".join(" " if self._read[i] else ch for i, ch in enumerate(self.text))
        return re.findall(r"\S*\d\S*", rest)


def _header(text: str) -> str:
    """The first line without its codes (one-line messages): where loader and date live."""
    first = text.split("\n", 1)[0]
    cut = [m.start() for m in (pattern.search(first) for pattern in _TOKENS_RE) if m]
    return first[: min(cut)] if cut else first


def _header_leftovers(header: str) -> list[str]:
    """Numbers in the first line other than the loader and the date."""
    rest = _LOADER_RE.sub(" ", header, count=1)
    for _, pattern in _DATE_PATTERNS:
        if pattern.search(rest):
            rest = pattern.sub(" ", rest, count=1)
            break
    else:
        rest = _SHORT_DATE_RE.sub(" ", rest, count=1)
    return re.findall(r"\S*\d\S*", rest)


def _short_date(candidate: str, today: date, errors: list[str]) -> date | None:
    short = _SHORT_DATE_RE.search(candidate)
    if not short:
        return None
    day, month = int(short["d"]), int(short["m"])
    try:
        found = _build_date(today.year, month, day)
    except ValueError:
        errors.append(f"Date: '{short.group(0)}' is not a valid date")
        return None
    # "06-11" typed in early January means last year's November
    return found if found <= today else _build_date(today.year - 1, month, day)


def _parse_date(header: str, body: _Body, today: date, errors: list[str]) -> date | None:
    labelled = _DATE_LABEL_RE.search(body.text)
    candidate = labelled.group("value") if labelled else header
    if labelled:
        body.take(labelled.start(), labelled.end())
    try:
        full = parse_date_value(candidate)
    except ValueError:
        errors.append(f"Date: '{candidate.strip()}' is not a valid date")
        return None
    reported = len(errors)
    found = full or _short_date(candidate, today, errors)
    if found or len(errors) > reported:
        return found
    if labelled:
        errors.append(f"Date: could not read '{labelled.group('value').strip()}' (use 10-11 or 10-11-2026)")
    else:
        errors.append("Date missing — put it after the loader: 'L11 10-11'")
    return None


def _parse_loader(header: str, body: _Body, errors: list[str]) -> int | None:
    match = _LOADER_RE.search(header)
    if match is None:
        for line in re.finditer(r"(?im)^[ \t]*(?:loader|ldr)\b[^\n]*", body.text):
            match = _LOADER_RE.search(line.group(0))
            if match:
                body.take(line.start(), line.end())
                break
    if match is None:
        errors.append("Loader missing — start with 'L11' (L and the loader number)")
        return None
    return int(match["num"])


def _put(values: dict, key: int, value, what: str, errors: list[str]) -> None:
    if key in values and values[key] != value:
        errors.append(f"{what} given twice ({values[key]} and {value})")
    else:
        values[key] = value


def _parse_shift_lines(body: _Body, trips: dict[int, int], errors: list[str]) -> None:
    for match in _SHIFT_RE.finditer(body.text):
        body.take(match.start(), match.end())
        shift = _SHIFT_TOKENS[match["ord"].lower()] if match["ord"] else int(match["num"])
        label, value = _LABEL[shift], match["val"]
        if value is None:
            errors.append(f"{label} shift: no trips number after it (e.g. 'T{shift} 36')")
        elif not _NIL_RE.match(value):
            try:
                _put(trips, shift, parse_int(value), f"{label} shift trips", errors)
            except ValueError as exc:
                errors.append(f"{label} shift: {exc}")


def _parse_codes(body: _Body, card: ParsedCard, errors: list[str]) -> None:
    for match in _CODE_RE.finditer(body.text):
        body.take(match.start(), match.end())
        code, shift, value = match["code"].upper(), int(match["shift"]), match["val"]
        name = {"T": "trips", "O": "opening HM", "C": "closing HM"}[code]
        if value is None:
            errors.append(f"{code}{shift}: no number after it (e.g. '{code}{shift} {'36' if code == 'T' else '4268'}')")
            continue
        if _NIL_RE.match(value):
            continue
        try:
            if code == "T":
                _put(card.trips, shift, parse_int(value), f"{_LABEL[shift]} shift trips", errors)
            else:
                _put(card.opening if code == "O" else card.closing, shift, parse_decimal(value), f"{code}{shift} ({name})", errors)
        except ValueError as exc:
            errors.append(f"{code}{shift} ({name}): {exc}")


def _parse_total_trips(body: _Body, card: ParsedCard, errors: list[str]) -> None:
    for match in _TOTAL_TRIPS_RE.finditer(body.text):
        if body.is_read(match.start()):
            continue  # part of a shift line ("1st shift trips - 36")
        body.take(match.start(), match.end())
        if _NIL_RE.match(match["val"]):
            continue
        try:
            value = parse_int(match["val"])
        except ValueError as exc:
            errors.append(f"T (total trips): {exc}")
            continue
        if card.total_trips is not None and card.total_trips != value:
            errors.append(f"T (total trips) given twice ({card.total_trips} and {value})")
        card.total_trips = value


def _parse_refills(body: _Body, refills: list[Refill], errors: list[str]) -> None:
    for match in _REFILL_RE.finditer(body.text):
        body.take(match.start(), match.end())
        if match["litres"] is None:
            errors.append("H: litres missing (e.g. 'H 132.4 4276.2' = 132.4 L at HM 4276.2)")
            continue
        try:
            litres = parse_decimal(match["litres"])
            hm = parse_decimal(match["hm"]) if match["hm"] else None
        except ValueError as exc:
            errors.append(f"H: {exc}")
            continue
        if litres <= 0:
            errors.append(f"H: litres must be more than 0, got '{match['litres']}'")
            continue
        if hm is None:
            errors.append(f"H {match['litres']}: HM missing — write the hour meter after the litres ('H {match['litres']} 4276.2')")
            continue
        refills.append(Refill(litres, hm))


def _parse_hsd_lines(body: _Body, refills: list[Refill], errors: list[str]) -> None:
    """Long form: ``HSD: 190 + 154.98`` (litres of each fill, no hour meter)."""
    for match in _HSD_RE.finditer(body.text):
        rest = match["rest"]
        nxt = [m.start() for m in (p.search(rest) for p in (_SHIFT_RE, _CODE_RE)) if m]
        rest = rest[: min(nxt)] if nxt else rest
        body.take(match.start(), match.start("rest") + len(rest))
        rest = _HSD_UNIT_RE.sub(" ", re.sub(r"\([^)]*\)", " ", rest)).strip(" \t:=-.>")  # "HSD (L): 190 L"
        parts = [p.strip() for p in _HSD_SPLIT_RE.split(rest) if p.strip()]
        if not parts:
            errors.append("HSD: no litres after it (e.g. 'HSD: 190 + 154.98', or 'H 190 4268' with the HM)")
            continue
        for part in parts:
            if _NIL_RE.match(part):
                continue
            if re.search(r"\d\s+\d", part):
                errors.append(f"HSD: '{part}' — put + between fills, or use 'H <litres> <HM>' for a refill with its HM")
                continue
            try:
                value = parse_decimal(part)
            except ValueError as exc:
                errors.append(f"HSD: {exc}")
                continue
            if value <= 0:
                errors.append(f"HSD: '{part}' must be more than 0")
                continue
            refills.append(Refill(value, None))


def parse_card(raw_text: str, *, today: date) -> ParsedCard:
    text = normalize_text(raw_text or "")
    errors: list[str] = []
    header = _header(text)
    body = _Body(text[len(header):])  # the loader/date header must not be read as a value
    loader = _parse_loader(header, body, errors)
    entry_date = _parse_date(header, body, today, errors)
    card = ParsedCard(loader_number=loader or 0, entry_date=entry_date or today)
    _parse_hsd_lines(body, card.refills, errors)
    _parse_shift_lines(body, card.trips, errors)
    _parse_codes(body, card, errors)
    _parse_total_trips(body, card, errors)
    _parse_refills(body, card.refills, errors)
    errors += [f"Could not read '{token}'" for token in _header_leftovers(header) + body.unread_numbers()]
    if not errors and not (card.trips or card.opening or card.closing or card.refills or card.total_trips is not None):
        errors.append("Nothing to save — add e.g. 'T1 36', 'O1 4268', 'C1 4276.2' or 'H 132.4 4276.2'")
    if entry_date and entry_date > today:
        errors.append(f"Date {entry_date:%d-%m-%Y} is in the future")
    if errors:
        raise CardParseError(errors)
    return card
