"""Deterministic parser for forwarded WhatsApp loader reports.

Expected shape (formatting varies)::

    *LOADER REPORT - CHP*
    *DATE*:- 06/11/2026
    *SHIFT*:- 1st Shift
    *LOADER - 01*
    Trip:- 42 Qty:-1340.840
    *PC - 15*
    Trip:- Nil Qty:- Nil
    *Total Trip*:-106
    *Total Qty*:-3449.150

The parser never guesses: an unreadable value is reported as an error for that loader, and a
report without a clear date, shift or equipment section is rejected as a whole.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.schemas.report import EquipmentEntry, LoaderEntry, ParsedReport


class ReportParseError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


# --- Text normalisation ---------------------------------------------------------------------

_DASHES = "‐‑‒–—―−﹘﹣－"
_REMOVE = "​‌‍⁠﻿‎‏*_~`\\"
_TRANSLATION = {ord(ch): "-" for ch in _DASHES} | {ord(ch): None for ch in _REMOVE}
_TRANSLATION |= {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"'}


def normalize_text(text: str) -> str:
    """Strip WhatsApp formatting (*bold*, _italic_), unify dashes and collapse whitespace."""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATION)
    lines = (re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.replace("\r", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


# --- Shared patterns --------------------------------------------------------------------------

_SEP = r"[\s:=\->]*"
_NIL_WORDS = r"(?:nil+|null|none|n\s*/\s*a|na)(?![a-z])"
_VALUE = rf"(?:\d[\d.,]*|{_NIL_WORDS})"
_NIL_RE = re.compile(rf"^{_NIL_WORDS}$", re.I)

_SHIFT_WORD = r"(?:shift|shipt|shft|shif|sift|shitf|shfit)"

_DATE_LABEL_RE = re.compile(rf"\bdate\b\.?{_SEP}(?P<value>[^\n]*)", re.I)
_SHIFT_LABEL_RE = re.compile(rf"\b{_SHIFT_WORD}\b[ \t]*[:=\-.>]+[ \t:=\-.>]*(?P<value>[^\n]+)", re.I)
_SHIFT_INLINE_RE = re.compile(
    rf"\b(?P<tok>[1-3]|iii|ii|i|first|second|third)\s*(?:st|nd|rd|th)?[\s.\-]*{_SHIFT_WORD}\b"
    rf"|\b{_SHIFT_WORD}\s*(?:no\.?)?[\s#\-]*(?P<tok2>[1-3])\b",
    re.I,
)
_SHIFT_VALUE_RE = re.compile(
    r"^\s*(?:no\.?\s*)?(?P<tok>[1-3]|iii|ii|i|first|second|third|one|two|three)\s*(?:st|nd|rd|th)?(?![a-z0-9])",
    re.I,
)
_SHIFT_TOKENS = {
    "1": 1, "2": 2, "3": 3, "i": 1, "ii": 2, "iii": 3,
    "first": 1, "second": 2, "third": 3, "one": 1, "two": 2, "three": 3,
}

_EQUIPMENT_RE = re.compile(
    r"\b(?P<kind>loader|ldr|pc)(?![a-z])\.?\s*(?:no\.?|number|#)?\s*[-:#.]*\s*(?P<num>\d{1,3})(?!\d)",
    re.I,
)
_TOTAL_RE = re.compile(r"\btotal\b", re.I)
_TRIP_RE = re.compile(rf"\btrips?\b\.?{_SEP}(?P<val>{_VALUE})?", re.I)
_QTY_RE = re.compile(
    rf"\b(?:qty|qnty|qnt|quantity|quantities|wt|weight|tonnage)\b\.?{_SEP}(?P<val>{_VALUE})?", re.I
)
_TOTAL_TRIP_RE = re.compile(rf"\btotal\s*(?:no\.?\s*of\s*)?trips?\b\.?{_SEP}(?P<val>{_VALUE})?", re.I)
_TOTAL_QTY_RE = re.compile(
    rf"\btotal\s*(?:qty|qnty|qnt|quantity|wt|weight|tonnage)\b\.?{_SEP}(?P<val>{_VALUE})?", re.I
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAME = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS = [
    # ISO first so 2026-11-06 is not read as day=20
    ("ymd", re.compile(r"(?<!\d)(?P<y>\d{4})\s*[/\-.]\s*(?P<m>\d{1,2})\s*[/\-.]\s*(?P<d>\d{1,2})(?!\d)")),
    ("dmy", re.compile(r"(?<!\d)(?P<d>\d{1,2})\s*[/\-.]\s*(?P<m>\d{1,2})\s*[/\-.]\s*(?P<y>\d{4}|\d{2})(?!\d)")),
    ("dMy", re.compile(rf"(?<!\d)(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s\-/.,]*{_MONTH_NAME}[\s\-/.,]*(?P<y>\d{{4}}|\d{{2}})(?!\d)", re.I)),
    ("Mdy", re.compile(rf"\b{_MONTH_NAME}[\s\-/.]*(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s,\-/.]+(?P<y>\d{{4}})(?!\d)", re.I)),
]


# --- Value helpers ----------------------------------------------------------------------------


def parse_decimal(token: str) -> Decimal:
    """Parse a number like ``1340.840`` or ``1,340.840``. Ambiguous forms raise ValueError."""
    t = token.strip().rstrip(".,")
    if "," in t:
        if "." in t and t.rfind(".") > t.rfind(","):
            t = t.replace(",", "")  # thousands grouping, e.g. 1,340.840 or 1,34,084.000
        else:
            raise ValueError(f"ambiguous number '{token}' (use a dot as the decimal separator)")
    if t.count(".") > 1 or not t:
        raise ValueError(f"unreadable number '{token}'")
    try:
        value = Decimal(t)
    except InvalidOperation as exc:
        raise ValueError(f"unreadable number '{token}'") from exc
    if not value.is_finite() or value < 0:
        raise ValueError(f"unreadable number '{token}'")
    return value


def parse_int(token: str) -> int:
    value = parse_decimal(token)
    if value != value.to_integral_value():
        raise ValueError(f"trips must be a whole number, got '{token}'")
    return int(value)


def _build_date(y: int, m: int, d: int) -> date:
    if y < 100:
        y += 2000
    if not 2000 <= y <= 2100:
        raise ValueError(f"year {y} is out of range")
    return date(y, m, d)  # raises ValueError for impossible dates such as 31/02


def parse_date_value(text: str) -> date | None:
    """Day-first date parsing: 06/11/2026, 06-11-2026, 6.11.26, 2026-11-06, 06 Nov 2026, Nov 6, 2026."""
    for kind, pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        g = match.groupdict()
        month = _MONTHS[g["mon"][:3].lower()] if g.get("mon") else int(g["m"])
        return _build_date(int(g["y"]), month, int(g["d"]))
    return None


def parse_shift_value(text: str) -> int | None:
    match = _SHIFT_VALUE_RE.match(text)
    if not match:
        return None
    return _SHIFT_TOKENS[match.group("tok").lower()]


# --- Parser -------------------------------------------------------------------------------------


@dataclass
class _Field:
    value: str | None = None
    is_nil: bool = False
    present: bool = False


def _read_field(pattern: re.Pattern[str], text: str) -> _Field:
    match = pattern.search(text)
    if not match:
        return _Field()
    token = match.group("val")
    if token is None:
        return _Field(present=True)
    if _NIL_RE.match(token.strip()):
        return _Field(is_nil=True, present=True)
    return _Field(value=token, present=True)


def _extract_date(text: str, errors: list[str]) -> date | None:
    labelled = [m.group("value") for m in _DATE_LABEL_RE.finditer(text)]
    candidates: set[date] = set()
    for value in labelled:
        try:
            parsed = parse_date_value(value)
        except ValueError as exc:
            errors.append(f"Invalid DATE '{value.strip()}': {exc}")
            return None
        if parsed:
            candidates.add(parsed)
    if not labelled:
        for line in text.split("\n"):
            try:
                parsed = parse_date_value(line)
            except ValueError:
                continue
            if parsed:
                candidates.add(parsed)
    if not candidates:
        errors.append("DATE not found (expected e.g. 'DATE:- 06/11/2026')")
        return None
    if len(candidates) > 1:
        shown = ", ".join(sorted(d.strftime("%d-%m-%Y") for d in candidates))
        errors.append(f"Report contains more than one date ({shown})")
        return None
    return candidates.pop()


def _extract_shift(text: str, errors: list[str]) -> int | None:
    found: set[int] = set()
    unreadable: list[str] = []
    for match in _SHIFT_LABEL_RE.finditer(text):
        value = match.group("value").strip()
        shift = parse_shift_value(value)
        if shift is None:
            unreadable.append(value)
        else:
            found.add(shift)
    if not found:
        for match in _SHIFT_INLINE_RE.finditer(text):
            token = (match.group("tok") or match.group("tok2")).lower()
            found.add(_SHIFT_TOKENS[token])
    if len(found) == 1:
        return found.pop()
    if len(found) > 1:
        errors.append("Report mentions more than one shift (" + ", ".join(map(str, sorted(found))) + ")")
    elif unreadable:
        errors.append(f"Unrecognised SHIFT '{unreadable[0]}' (expected 1st, 2nd or 3rd shift)")
    else:
        errors.append("SHIFT not found (expected e.g. 'SHIFT:- 1st Shift')")
    return None


def _segment_bounds(text: str) -> tuple[list[re.Match[str]], list[int]]:
    headers = list(_EQUIPMENT_RE.finditer(text))
    stops = {m.start() for m in headers}
    stops |= {m.start() for m in _TOTAL_RE.finditer(text)}
    stops |= {m.start() for m in _DATE_LABEL_RE.finditer(text)}
    stops |= {m.start() for m in _SHIFT_LABEL_RE.finditer(text)}
    stops.add(len(text))
    return headers, sorted(stops)


def _parse_figures(segment: str) -> dict:
    trips_f = _read_field(_TRIP_RE, segment)
    qty_f = _read_field(_QTY_RE, segment)
    values: dict = {"trips_nil": trips_f.is_nil, "quantity_nil": qty_f.is_nil}
    problems: list[str] = []
    if trips_f.value is not None:
        try:
            values["trips"] = parse_int(trips_f.value)
        except ValueError as exc:
            problems.append(f"Trip: {exc}")
    if qty_f.value is not None:
        try:
            values["quantity_mt"] = parse_decimal(qty_f.value)
        except ValueError as exc:
            problems.append(f"Qty: {exc}")
    if problems:
        values["error"] = "; ".join(problems)
    values["_found"] = trips_f.present or qty_f.present
    return values


def parse_report(raw_text: str) -> ParsedReport:
    """Parse a forwarded report. Raises ReportParseError when it cannot be read safely."""
    if not raw_text or not raw_text.strip():
        raise ReportParseError(["Message is empty"])
    text = normalize_text(raw_text)
    errors: list[str] = []
    warnings: list[str] = []

    report_date = _extract_date(text, errors)
    shift = _extract_shift(text, errors)

    headers, stops = _segment_bounds(text)
    loaders: list[dict] = []
    others: list[dict] = []
    for header in headers:
        end = next(s for s in stops if s > header.start())
        segment = text[header.end():end]
        figures = _parse_figures(segment)
        found = figures.pop("_found")
        kind = header.group("kind").lower()
        number = int(header.group("num"))
        label = f"{'Loader' if kind != 'pc' else 'PC'} {number:02d}"
        if not found:
            warnings.append(f"{label}: no Trip/Qty values found")
        if kind == "pc":
            others.append({"category": "PC", "number": number, **figures})
        elif number == 0:
            warnings.append("Ignored 'Loader 00' (not a valid loader number)")
        else:
            loaders.append({"loader_number": number, **figures})

    if not headers:
        errors.append("No loader sections found (expected e.g. 'LOADER - 01' followed by 'Trip:- 42 Qty:-1340.840')")

    counts = Counter(entry["loader_number"] for entry in loaders)
    for entry in loaders:
        if counts[entry["loader_number"]] > 1:
            entry["error"] = f"listed {counts[entry['loader_number']]} times in the report"

    total_trips = _read_field(_TOTAL_TRIP_RE, text)
    total_qty = _read_field(_TOTAL_QTY_RE, text)
    reported_total_trips = reported_total_qty = None
    if total_trips.value is not None:
        try:
            reported_total_trips = parse_int(total_trips.value)
        except ValueError as exc:
            warnings.append(f"Total Trip: {exc}")
    if total_qty.value is not None:
        try:
            reported_total_qty = parse_decimal(total_qty.value)
        except ValueError as exc:
            warnings.append(f"Total Qty: {exc}")

    if errors:
        raise ReportParseError(errors)
    assert report_date is not None and shift is not None

    unique_loaders: dict[int, LoaderEntry] = {}
    for entry in loaders:
        unique_loaders.setdefault(entry["loader_number"], LoaderEntry(**entry))
    return ParsedReport(
        report_date=report_date,
        shift=shift,
        loaders=sorted(unique_loaders.values(), key=lambda e: e.loader_number),
        other_equipment=[EquipmentEntry(**o) for o in others],
        reported_total_trips=reported_total_trips,
        reported_total_quantity_mt=reported_total_qty,
        warnings=warnings,
    )
