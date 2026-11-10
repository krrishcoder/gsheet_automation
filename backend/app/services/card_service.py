"""Card messages (trips, hour meter, diesel refills): preview against the sheet, write after Confirm.

One message can fill three of a loader's blocks, and only the cells for the values it carries, so
a day can be filled shift by shift:

* card block ("Loader 11 Card Details"): ``T1``-``T3`` trips, and HSD Fuel (L) = the day's fills
* HM block ("Loader 11 Opening and Closing"): ``O1``-``O3`` / ``C1``-``C3`` hour meter readings
* refill log ("Loader 11 hsd avg"): one new row per ``H <litres> <HM>`` refill — Date (written by
  the bot), hsd, hm, and the avg formula ``=hsd/(hm - previous hm)``

Rules:
* Trips and HM readings are absolute: a different value already in a cell is shown as a change.
* HSD Fuel holds the day's fills as a sum (``=190+154.98``); fills already in it are not added
  again. The log skips a refill whose litres and HM are already logged. Re-sending a message
  therefore never double counts.
* Closing HM below opening HM, or a refill HM that is not after the last refill, is refused;
  a shift over 8 hours or an opening HM that differs from the previous closing is a warning.

States: PENDING_CONFIRMATION | BLOCKED | UNCHANGED -> QUEUED -> APPLYING -> UPDATED | FAILED | CONFLICT.
Every previewed cell is re-checked under the tab lock before writing.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.core.config import CorrectionPolicy, Settings
from app.database.database import Database
from app.database.models import CardStatus, utcnow
from app.database.repositories import CardRepository, LoaderRepository, UserRepository, audit
from app.schemas.sheets import CellValue, CellWrite, SheetGrid
from app.services.authorization_service import AuthorizationService
from app.services.google_sheets_service import SheetsClient, SheetsError
from app.services.locks import LockManager, LockTimeout
from app.services.report_processor import TransientApplyError
from app.services.sheet_layout import (
    Block,
    CellValueError,
    SheetLayoutError,
    cell_to_date,
    cell_to_decimal,
    cell_to_int,
    column_letter,
    find_date_row,
    locate_block,
    locate_table,
    read_row_state,
)
from app.services.worksheet_service import WorksheetResolutionError, resolve_worksheet

log = logging.getLogger(__name__)

CARD_IN_FLIGHT = {CardStatus.QUEUED, CardStatus.APPLYING}
MAX_SHIFT_HOURS = Decimal(8)
DATE_PATTERN = "dd-mm-yyyy"  # as the rest of the sheet shows dates
_SUM_FORMULA = r"^=\s*\d+(?:\.\d+)?(?:\s*\+\s*\d+(?:\.\d+)?)*\s*$"
_LABEL = {1: "1st", 2: "2nd", 3: "3rd"}
_SHEETS_EPOCH = date(1899, 12, 30)


class CardBlocked(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fmt_dec(value: Decimal) -> str:
    """190.00 -> "190", 154.980 -> "154.98" (no exponent)."""
    return format(value.normalize(), "f")


def cell_state(kind: str, cell: CellValue, ref: str) -> Any:
    """What a cell holds, in a form that can be compared between preview and write."""
    if kind == "trips":
        return cell_to_int(cell, what=ref)
    if cell.formula:
        return cell.formula.replace(" ", "")
    if kind == "date":
        found = cell_to_date(cell)
        return found.isoformat() if found else (None if cell.is_empty else cell.text)
    value = cell_to_decimal(cell, what=ref)
    return None if value is None else fmt_dec(value)


def sum_fills(state: str | None, ref: str) -> list[Decimal]:
    """Fills in an HSD Fuel cell: empty, a number, or a sum such as =190+154.98."""
    if state is None:
        return []
    if state.startswith("="):
        if not re.match(_SUM_FORMULA, state):
            raise CardBlocked("HSD_FORMULA", f"{ref} has a formula ({state}) that is not a sum of litres; not changing it")
        return [Decimal(part) for part in state[1:].split("+")]
    return [Decimal(state)]


def _dec(state: Any) -> Decimal | None:
    if state is None or (isinstance(state, str) and state.startswith("=")):
        return None
    return Decimal(str(state))


@dataclass
class CardPlan:
    cells: dict[str, dict[str, Any]] = field(default_factory=dict)  # ref -> what was seen / is written
    rows: dict[str, int] = field(default_factory=dict)  # block kind -> date row (card, hm)
    blocks: dict[str, str] = field(default_factory=dict)  # block kind -> title
    day: dict[str, Any] = field(default_factory=dict)  # the card row after saving (summary)
    hours: dict[str, Any] = field(default_factory=dict)  # shift -> {open, close, hours} after saving
    refills: list[dict[str, Any]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)  # filled cells that would get a different value
    warnings: list[str] = field(default_factory=list)

    @property
    def writes(self) -> list[str]:
        return [ref for ref, c in self.cells.items() if c["before"] != c["after"]]

    def add(self, block: Block, key: str, row: int, kind: str, before: Any, after: Any, write: Any, **extra: Any) -> None:
        self.cells[block.ref(key, row)] = {
            "block": block.kind, "key": key, "row": row, "kind": kind, "before": before, "after": after, "write": write, **extra
        }


def _read(block: Block, key: str, row: int, kind: str, *, needed: bool) -> Any:
    """A cell's state; an unreadable cell only matters when it is going to be written."""
    try:
        return cell_state(kind, block.grid.cell(row, block.columns[key]), block.ref(key, row))
    except CellValueError as exc:
        if needed:
            raise CardBlocked("CELL_VALUE_INVALID", str(exc)) from exc
        return None


def _value_cell(plan: CardPlan, block: Block, key: str, row: int, kind: str, value: Any, write: Any) -> Any:
    """Plan an absolute value (trips or HM reading) and return what the cell holds now."""
    ref = block.ref(key, row)
    if block.grid.cell(row, block.columns[key]).formula:
        raise CardBlocked("TARGET_HAS_FORMULA", f"{ref} contains a formula; not overwriting it")
    before = _read(block, key, row, kind, needed=True)
    plan.add(block, key, row, kind, before, value, write)
    if before is not None and before != value:
        plan.changes.append(f"{ref} {before} → {value}")
    return before


def plan_card_block(plan: CardPlan, block: Block, row: int, trips: dict[int, int], fills: list[Decimal]) -> None:
    plan.rows["card"], plan.blocks["card"] = row, block.region.title or ""
    for shift in (1, 2, 3):
        key = f"trips{shift}"
        if shift in trips:
            _value_cell(plan, block, key, row, "trips", trips[shift], trips[shift])
            plan.cells[block.ref(key, row)]["shift"] = shift
            plan.day[f"t{shift}"] = trips[shift]
        else:
            plan.day[f"t{shift}"] = _read(block, key, row, "trips", needed=False)
    if "hsd" not in block.columns:
        if fills:
            raise CardBlocked("NO_HSD_COLUMN", f"'{block.region.title}' has no 'HSD Fuel (L)' column")
        return
    before = _read(block, "hsd", row, "hsd", needed=bool(fills))
    existing = sum_fills(before, block.ref("hsd", row)) if fills else []
    if not fills:
        try:
            plan.day["hsd"] = [fmt_dec(f) for f in sum_fills(before, "")]
        except CardBlocked:
            plan.day["hsd"] = None  # some other formula
        return
    remaining = Counter(existing)
    added, already = [], []
    for fill in fills:
        if remaining[fill] > 0:
            remaining[fill] -= 1
            already.append(fill)
        else:
            added.append(fill)
    after_fills = existing + added
    if len(after_fills) == 1:
        after, write = fmt_dec(after_fills[0]), float(after_fills[0])
    else:
        after = "=" + "+".join(fmt_dec(f) for f in after_fills)
        write = after
    plan.add(
        block, "hsd", row, "hsd", before, after if added else before, write,
        existing=[fmt_dec(f) for f in existing], added=[fmt_dec(f) for f in added], already=[fmt_dec(f) for f in already],
    )
    plan.day["hsd"] = [fmt_dec(f) for f in after_fills]


def plan_hm_block(plan: CardPlan, block: Block, row: int, opening: dict[int, Decimal], closing: dict[int, Decimal]) -> None:
    plan.rows["hm"], plan.blocks["hm"] = row, block.region.title or ""
    after: dict[str, Decimal | None] = {}
    for shift in (1, 2, 3):
        for key, sent in ((f"open{shift}", opening.get(shift)), (f"close{shift}", closing.get(shift))):
            if sent is not None:
                _value_cell(plan, block, key, row, "hm", fmt_dec(sent), float(sent))
                plan.cells[block.ref(key, row)]["shift"] = shift
                after[key] = sent
            else:
                after[key] = _dec(_read(block, key, row, "hm", needed=False))
    previous_close = None  # the 3rd shift's closing HM on the day before
    if row - 1 >= block.layout.data_start_row:
        previous_close = _dec(_read(block, "close3", row - 1, "hm", needed=False))
    for shift in (1, 2, 3):
        o, c = after[f"open{shift}"], after[f"close{shift}"]
        label = f"{_LABEL[shift]} shift"
        sent = shift in opening or shift in closing
        if sent and o is not None and c is not None:
            if c < o:
                raise CardBlocked("HM_BACKWARDS", f"{label}: closing HM {fmt_dec(c)} is below opening HM {fmt_dec(o)}")
            if c - o > MAX_SHIFT_HOURS:
                plan.warnings.append(f"{label} ran {fmt_dec(c - o)} h by the hour meter (more than {MAX_SHIFT_HOURS})")
        prev = after[f"close{shift - 1}"] if shift > 1 else previous_close
        touched = shift in opening or (shift > 1 and (shift - 1) in closing)
        if touched and o is not None and prev is not None and o != prev:
            what = f"{_LABEL[shift - 1]} shift's closing" if shift > 1 else "previous day's 3rd-shift closing"
            plan.warnings.append(f"{label} opening HM {fmt_dec(o)} is not the {what} HM {fmt_dec(prev)}")
        plan.hours[str(shift)] = {
            "open": None if o is None else fmt_dec(o),
            "close": None if c is None else fmt_dec(c),
            "hours": fmt_dec(c - o) if o is not None and c is not None else None,
        }


def plan_refill_log(plan: CardPlan, block: Block, entry_date: date, refills: list[tuple[Decimal, Decimal]]) -> None:
    plan.blocks["log"] = block.region.title or ""
    first = block.layout.data_start_row
    used = ("date", "hsd", "hm")
    last_row, last_hm = first - 1, None
    logged: Counter = Counter()
    for r in range(first, block.grid.last_row + 1):
        if all(block.grid.cell(r, block.columns[k]).is_empty for k in used):
            continue
        last_row = r
        litres = _dec(_read(block, "hsd", r, "num", needed=False))
        hm = _dec(_read(block, "hm", r, "num", needed=False))
        if hm is not None:
            last_hm = (hm, r)
        if litres is not None and hm is not None:
            logged[(litres, hm)] += 1
    for litres, hm in sorted(refills, key=lambda x: x[1]):
        entry = {"litres": fmt_dec(litres), "hm": fmt_dec(hm)}
        if logged[(litres, hm)] > 0:
            logged[(litres, hm)] -= 1
            plan.refills.append({**entry, "state": "already"})
            continue
        if last_hm is not None and hm <= last_hm[0]:
            raise CardBlocked(
                "HM_NOT_INCREASING",
                f"Refill at HM {fmt_dec(hm)} is not after the last refill (HM {fmt_dec(last_hm[0])}, row {last_hm[1]}) — check the HM",
            )
        row = last_row + 1
        if block.bounded and row > block.region.last_row:
            raise CardBlocked("LOG_FULL", f"'{block.region.title}' is full (row {row} belongs to the next table)")
        serial = (entry_date - _SHEETS_EPOCH).days
        plan.add(block, "date", row, "date", None, entry_date.isoformat(), serial, date_format=DATE_PATTERN)
        plan.add(block, "hsd", row, "num", None, entry["litres"], float(litres))
        plan.add(block, "hm", row, "num", None, entry["hm"], float(hm))
        avg = None
        if last_hm is not None:
            avg = fmt_dec((litres / (hm - last_hm[0])).quantize(Decimal("0.001")))
            # same formula as the hand-typed rows: litres / HM run since the refill in the row above
            if "avg" in block.columns and last_hm[1] == row - 1 and block.grid.cell(row, block.columns["avg"]).is_empty:
                hsd_col, hm_col = column_letter(block.columns["hsd"]), column_letter(block.columns["hm"])
                formula = f"={hsd_col}{row}/({hm_col}{row}-{hm_col}{row - 1})"
                plan.add(block, "avg", row, "avg", None, formula, formula)
        plan.refills.append({**entry, "state": "new", "row": row, "avg": avg})
        last_row, last_hm = row, (hm, row)


@dataclass
class _Input:
    loader_number: int
    entry_date: date
    trips: dict[int, int]
    opening: dict[int, Decimal]
    closing: dict[int, Decimal]
    fills: list[Decimal]  # every fill, for HSD Fuel
    refills: list[tuple[Decimal, Decimal]]  # fills with their HM, for the log

    @classmethod
    def of(cls, card) -> "_Input":
        hm = card.hm_readings or {}
        return cls(
            loader_number=card.loader_number,
            entry_date=card.entry_date,
            trips={int(k): v for k, v in (card.trips or {}).items()},
            opening={int(k[1:]): Decimal(v) for k, v in hm.items() if k.startswith("o")},
            closing={int(k[1:]): Decimal(v) for k, v in hm.items() if k.startswith("c")},
            fills=[Decimal(f) for f in card.hsd_fills or []],
            refills=[(Decimal(r["litres"]), Decimal(r["hm"])) for r in card.refills or [] if r.get("hm") is not None],
        )

    @property
    def needs(self) -> list[str]:
        kinds = []
        hsd_without_hm = len(self.fills) > len(self.refills)
        if self.trips or hsd_without_hm:
            kinds.append("card")
        if self.opening or self.closing:
            kinds.append("hm")
        if self.refills:
            kinds.append("log")
        return kinds


class CardService:
    def __init__(
        self, db: Database, sheets: SheetsClient, locks: LockManager, settings: Settings, auth: AuthorizationService
    ) -> None:
        self.db = db
        self.sheets = sheets
        self.locks = locks
        self.settings = settings
        self.auth = auth

    def _block(self, grid: SheetGrid, loader_number: int, kind: str) -> Block:
        return locate_block(
            grid, loader_number, kind, self.settings.sheet_column_map, scan_rows=self.settings.sheet_header_scan_rows
        )

    def plan(self, grid: SheetGrid, inp: _Input) -> CardPlan:
        """Every cell this entry would write, as the sheet is now. Raises SheetLayoutError / CardBlocked."""
        plan = CardPlan()
        needs = inp.needs
        blocks = {kind: self._block(grid, inp.loader_number, kind) for kind in needs}
        if "card" not in blocks and inp.fills:
            try:  # refills also add to the card block's HSD Fuel when the loader has one
                card = self._block(grid, inp.loader_number, "card")
            except SheetLayoutError:
                card = None
            if card is not None and "hsd" in card.columns:
                blocks["card"] = card
        if "card" in blocks:
            card = blocks["card"]
            plan_card_block(plan, card, find_date_row(card.grid, card.layout, inp.entry_date), inp.trips, inp.fills)
        if "hm" in blocks:
            hm = blocks["hm"]
            plan_hm_block(plan, hm, find_date_row(hm.grid, hm.layout, inp.entry_date), inp.opening, inp.closing)
        if "log" in blocks:
            plan_refill_log(plan, blocks["log"], inp.entry_date, inp.refills)
        return plan

    def _report_trips(self, grid: SheetGrid, loader_number: int, entry_date: date, shared: bool) -> dict[str, int | None] | None:
        """The CHP report's trips for that day (from the loader's shift table), for comparison only."""
        try:
            table, layout, _ = locate_table(
                grid,
                loader_number,
                self.settings.sheet_column_map,
                detection=self.settings.sheet_header_detection,
                strict=self.settings.sheet_header_strict,
                scan_rows=self.settings.sheet_header_scan_rows,
                require_title=shared,
            )
            state = read_row_state(table, layout, find_date_row(table, layout, entry_date))
        except (SheetLayoutError, CellValueError):
            return None
        return {str(n): v.trips for n, v in state.shifts.items()}

    # --- Preview ------------------------------------------------------------------------------

    def preview(self, card_id: int) -> CardStatus:
        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            if card is None:
                raise ValueError(f"card entry {card_id} not found")
            inp = _Input.of(card)
            n = inp.loader_number
            submitter = UserRepository(session).get_by_telegram_id(card.telegram_user_id)
            submitter_is_admin = self.auth.is_admin(submitter)
            loaders = LoaderRepository(session)
            loader = loaders.get_by_number(n)
            blocked: CardBlocked | None = None
            if loader is None:
                blocked = CardBlocked("LOADER_NOT_CONFIGURED", f"No spreadsheet configured for Loader {n:02d} (admin: /set_sheet {n} <URL>)")
            elif not loader.is_active:
                blocked = CardBlocked("LOADER_DISABLED", f"Loader {n:02d} is disabled")
            elif not self.auth.can_submit_for(session, submitter, loader):
                blocked = CardBlocked("UNAUTHORIZED", f"You are not authorised for Loader {n:02d}")
            else:
                card.loader_id = loader.id
                spreadsheet_id = loader.spreadsheet_id
                mapping = loaders.get_worksheet(loader.id, inp.entry_date.year, inp.entry_date.month)
                configured = mapping.worksheet_name if mapping and mapping.is_active else None
        if blocked:
            return self._set(card_id, CardStatus.BLOCKED, blocked.code, blocked.message)

        values: dict[str, Any] = {}
        try:
            info = self.sheets.get_spreadsheet_info(spreadsheet_id)
            resolution = resolve_worksheet(
                loader_number=n,
                year=inp.entry_date.year,
                month=inp.entry_date.month,
                configured_name=configured,
                available_titles=info.worksheet_titles,
                auto_detect=self.settings.worksheet_auto_detect,
            )
            values.update(spreadsheet_id=spreadsheet_id, worksheet_name=resolution.worksheet_name)
            grid = self.sheets.read_grid(spreadsheet_id, resolution.worksheet_name)
            plan = self.plan(grid, inp)
        except (SheetsError, WorksheetResolutionError, SheetLayoutError, CardBlocked) as exc:
            return self._set(card_id, CardStatus.BLOCKED, exc.code, exc.message, **values)

        values["sheet_row"] = plan.rows.get("card") or plan.rows.get("hm")
        values["preview"] = {
            "cells": plan.cells,
            "rows": plan.rows,
            "blocks": plan.blocks,
            "day": plan.day,
            "hours": plan.hours,
            "refills": plan.refills,
            "changes": plan.changes,
            "warnings": plan.warnings,
            "report_trips": self._report_trips(grid, n, inp.entry_date, resolution.shared) if inp.trips else None,
        }
        if not plan.writes:
            return self._set(card_id, CardStatus.UNCHANGED, "UNCHANGED", "The sheet already has these values — nothing to change", **values)
        if plan.changes:
            policy = self.settings.correction_policy
            changes = "; ".join(plan.changes)
            if policy == CorrectionPolicy.REJECT:
                return self._set(card_id, CardStatus.BLOCKED, "DUPLICATE_REJECTED", f"Already filled ({changes}); corrections are disabled", **values)
            if policy == CorrectionPolicy.ADMIN_APPROVAL and not submitter_is_admin:
                return self._set(card_id, CardStatus.BLOCKED, "NEEDS_ADMIN", f"Changing filled cells ({changes}) needs an administrator", **values)
        return self._set(card_id, CardStatus.PENDING_CONFIRMATION, None, None, **values)

    def _set(self, card_id: int, status: CardStatus, code: str | None, message: str | None, **values: Any) -> CardStatus:
        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            card.status = status
            card.error_code, card.error_message = code, message
            for key, value in values.items():
                setattr(card, key, value)
            if status not in (CardStatus.PENDING_CONFIRMATION, *CARD_IN_FLIGHT):
                card.completed_at = utcnow()
        return status

    # --- Apply --------------------------------------------------------------------------------

    def apply(self, card_id: int) -> CardStatus | None:
        """Write a confirmed entry. Raises TransientApplyError when it should be retried later."""
        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            if card is None or card.status not in CARD_IN_FLIGHT:
                return None
            card.attempts += 1
            loader = card.loader
            failure = None
            if loader is None or not loader.is_active:
                failure = (CardStatus.FAILED, "LOADER_DISABLED", "Loader was disabled before the update")
            elif loader.spreadsheet_id != card.spreadsheet_id:
                failure = (CardStatus.CONFLICT, "CONFIGURATION_CHANGED", "The loader's spreadsheet changed; please send it again")
            inp = _Input.of(card)
            spreadsheet_id, worksheet = card.spreadsheet_id, card.worksheet_name
            preview = dict(card.preview or {})
        if failure:
            return self._finish(card_id, *failure)

        try:
            with self.locks.hold(f"sheet:{spreadsheet_id}:{worksheet}"):
                return self._apply_locked(card_id, inp, spreadsheet_id, worksheet, preview)
        except LockTimeout as exc:
            self._note_transient(card_id, str(exc))
            raise TransientApplyError(str(exc)) from exc
        except SheetsError as exc:
            if exc.retryable:
                self._note_transient(card_id, exc.message)
                raise TransientApplyError(exc.message) from exc
            return self._finish(card_id, CardStatus.FAILED, exc.code, exc.message)
        except (SheetLayoutError, CardBlocked) as exc:
            return self._finish(card_id, CardStatus.FAILED, exc.code, exc.message)
        except CellValueError as exc:
            return self._finish(card_id, CardStatus.FAILED, "CELL_VALUE_INVALID", str(exc))

    def _apply_locked(self, card_id: int, inp: _Input, spreadsheet_id: str, worksheet: str, preview: dict) -> CardStatus:
        grid = self.sheets.read_grid(spreadsheet_id, worksheet)
        previewed: dict[str, dict] = preview.get("cells") or {}
        blocks = {kind: self._block(grid, inp.loader_number, kind) for kind in {c["block"] for c in previewed.values()}}
        for kind, row in (preview.get("rows") or {}).items():
            if kind not in blocks:
                continue
            now = find_date_row(blocks[kind].grid, blocks[kind].layout, inp.entry_date)
            if now != row:
                return self._finish(
                    card_id, CardStatus.CONFLICT, "ROW_MOVED", f"The row for {inp.entry_date:%d-%m-%Y} moved from {row} to {now}; please send it again"
                )
        current: dict[str, Any] = {}
        for ref, seen in previewed.items():
            block = blocks[seen["block"]]
            if seen["key"] not in block.columns or block.ref(seen["key"], seen["row"]) != ref:
                return self._finish(card_id, CardStatus.CONFLICT, "COLUMNS_MOVED", f"'{block.region.title}' changed its columns; please send it again")
            current[ref] = cell_state(seen["kind"], block.grid.cell(seen["row"], block.columns[seen["key"]]), ref)
            if current[ref] not in (seen["before"], seen["after"]):
                return self._finish(
                    card_id,
                    CardStatus.CONFLICT,
                    "SHEET_CHANGED",
                    f"{ref} changed since the preview (was {_show(seen['before'])}, now {_show(current[ref])}); please send it again",
                )
        # Cells already at their planned value (a re-run after a crash) are not written again, except
        # dates, whose display format is set together with the value.
        writes = [
            CellWrite(seen["row"], blocks[seen["block"]].columns[seen["key"]], seen["write"], date_format=seen.get("date_format"))
            for ref, seen in previewed.items()
            if current[ref] != seen["after"] or (seen["kind"] == "date" and seen["before"] != seen["after"])
        ]

        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            if card.previous_values is None:
                card.previous_values = {ref: seen["before"] for ref, seen in previewed.items()}
            card.updated_values = {ref: seen["after"] for ref, seen in previewed.items()}
            card.status = CardStatus.APPLYING
            card.error_code = card.error_message = None

        self.sheets.write_cells(spreadsheet_id, worksheet, writes, user_entered=True)

        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            card.status = CardStatus.UPDATED
            card.applied_at = card.completed_at = utcnow()
            audit(
                session,
                "card_row_updated",
                actor=card.confirmed_by,
                entity_type="card_entry",
                entity_id=card_id,
                loader_number=inp.loader_number,
                spreadsheet_id=spreadsheet_id,
                worksheet=worksheet,
                rows=preview.get("rows"),
                old=card.previous_values,
                new=card.updated_values,
            )
        log.info("card entry written", extra={"card_id": card_id, "worksheet": worksheet, "cells": len(writes)})
        return CardStatus.UPDATED

    def _finish(self, card_id: int, status: CardStatus, code: str, message: str) -> CardStatus:
        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            card.status, card.error_code, card.error_message = status, code, message
            card.completed_at = utcnow()
            audit(session, "card_update_not_applied", actor=None, entity_type="card_entry", entity_id=card_id, status=status, code=code, message=message)
        log.warning("card not applied", extra={"card_id": card_id, "status": status.value, "code": code})
        return status

    def _note_transient(self, card_id: int, message: str) -> None:
        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            card.error_code = "TEMPORARY_ERROR"
            card.error_message = f"Temporary error, will retry: {message}"

    def fail_in_flight(self, card_id: int, message: str) -> None:
        with self.db.session() as session:
            card = CardRepository(session).get(card_id)
            if card is not None and card.status in CARD_IN_FLIGHT:
                card.status, card.error_code, card.error_message = CardStatus.FAILED, "SHEETS_UNAVAILABLE", message
                card.completed_at = utcnow()


def _show(value: Any) -> str:
    return "empty" if value is None else str(value)
