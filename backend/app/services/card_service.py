"""Trip-card / HSD entries: preview against the loader's card block, write after Confirm.

A message may carry any of the three shifts and the day's HSD tanker fills; only those cells are
written, so a day can be filled shift by shift.

* Trips: the sent number is the shift's value (a different value already in the sheet is shown as
  a change before it is confirmed).
* HSD: the cell holds the day's fills as a sum (``=190+154.98``). Fills in the message that are not
  in the cell yet are added; fills already there are not added twice, so re-sending a message, or
  sending the full day after a partial one, never double counts.

States: PENDING_CONFIRMATION | BLOCKED | UNCHANGED -> QUEUED -> APPLYING -> UPDATED | FAILED | CONFLICT.
The previewed "before" of every target cell is re-checked under the tab lock before writing.
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
from app.database.models import CardEntry, CardStatus, utcnow
from app.database.repositories import CardRepository, LoaderRepository, UserRepository, audit
from app.schemas.sheets import CellValue, CellWrite, SheetGrid
from app.services.authorization_service import AuthorizationService
from app.services.google_sheets_service import SheetsClient, SheetsError
from app.services.locks import LockManager, LockTimeout
from app.services.report_processor import TransientApplyError
from app.services.sheet_layout import (
    CARD_TRIP_COLUMNS,
    CellValueError,
    Col,
    SheetLayoutError,
    cell_to_decimal,
    cell_to_int,
    column_index,
    column_letter,
    find_date_row,
    locate_card_table,
    locate_table,
    read_row_state,
)
from app.services.worksheet_service import WorksheetResolutionError, resolve_worksheet

log = logging.getLogger(__name__)

CARD_IN_FLIGHT = {CardStatus.QUEUED, CardStatus.APPLYING}
_SUM_FORMULA_RE = re.compile(r"^=\s*\d+(?:\.\d+)?(?:\s*\+\s*\d+(?:\.\d+)?)*\s*$")


class CardBlocked(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def fmt_dec(value: Decimal) -> str:
    """190.00 -> "190", 154.980 -> "154.98" (no exponent)."""
    return format(value.normalize(), "f")


def hsd_state(cell: CellValue) -> str | None:
    """Comparable content of an HSD cell: its formula, or its number, or None when empty."""
    if cell.formula:
        return cell.formula.replace(" ", "")
    value = cell_to_decimal(cell, what="HSD cell")
    return None if value is None else fmt_dec(value)


def hsd_fills(state: str | None, ref: str) -> list[Decimal]:
    if state is None:
        return []
    if state.startswith("="):
        if not _SUM_FORMULA_RE.match(state):
            raise CardBlocked("HSD_FORMULA", f"{ref} has a formula ({state}) that is not a sum of litres; not changing it")
        return [Decimal(part) for part in state[1:].split("+")]
    return [Decimal(state)]


@dataclass
class CardPlan:
    row: int
    cells: dict[str, dict[str, Any]] = field(default_factory=dict)  # ref -> what was seen and what is written
    day: dict[str, Any] = field(default_factory=dict)  # every shift + HSD after saving (for the summary)
    changes: list[str] = field(default_factory=list)  # existing trips values that would be replaced

    @property
    def writes(self) -> list[str]:
        return [ref for ref, c in self.cells.items() if c["before"] != c["after"]]


def plan_card(grid: SheetGrid, layout, row: int, trips: dict[int, int], fills: list[Decimal]) -> CardPlan:
    plan = CardPlan(row=row)
    for shift, logical in CARD_TRIP_COLUMNS.items():
        col = layout.col(logical)
        ref = f"{column_letter(col)}{row}"
        cell = grid.cell(row, col)
        try:
            before = cell_to_int(cell, what=ref)
        except CellValueError as exc:
            if shift in trips:
                raise CardBlocked("CELL_VALUE_INVALID", str(exc)) from exc
            before = None
        after = before
        if shift in trips:
            if cell.formula:
                raise CardBlocked("TARGET_HAS_FORMULA", f"{ref} contains a formula; not overwriting it")
            after = trips[shift]
            plan.cells[ref] = {"kind": "trips", "shift": shift, "before": before, "after": after, "write": after}
            if before is not None and before != after:
                plan.changes.append(f"{ref} {before} → {after}")
        plan.day[str(shift)] = after

    hsd_col = layout.columns.get(Col.HSD_FUEL)
    if fills and hsd_col is None:
        raise CardBlocked("NO_HSD_COLUMN", "The card block has no 'HSD Fuel (L)' column")
    if hsd_col is not None:
        ref = f"{column_letter(hsd_col)}{row}"
        try:
            before = hsd_state(grid.cell(row, hsd_col))
        except CellValueError as exc:
            if fills:
                raise CardBlocked("CELL_VALUE_INVALID", str(exc)) from exc
            before = None
        existing = hsd_fills(before, ref) if fills else []
        remaining = Counter(existing)
        added, already = [], []
        for fill in fills:
            if remaining[fill] > 0:
                remaining[fill] -= 1
                already.append(fill)
            else:
                added.append(fill)
        after_fills = existing + added
        if fills:
            if len(after_fills) == 1:
                after, write = fmt_dec(after_fills[0]), float(after_fills[0])
            else:
                after = "=" + "+".join(fmt_dec(f) for f in after_fills)
                write = after
            plan.cells[ref] = {
                "kind": "hsd",
                "before": before,
                "after": after if added else before,
                "write": write,
                "existing": [fmt_dec(f) for f in existing],
                "added": [fmt_dec(f) for f in added],
                "already": [fmt_dec(f) for f in already],
            }
            plan.day["hsd"] = [fmt_dec(f) for f in after_fills]
        else:
            try:
                plan.day["hsd"] = [fmt_dec(f) for f in hsd_fills(before, ref)]
            except CardBlocked:
                plan.day["hsd"] = None  # some other formula: shown as unknown
    return plan


@dataclass
class _Ctx:
    card_id: int
    loader_number: int
    entry_date: date
    trips: dict[int, int]
    fills: list[Decimal]
    spreadsheet_id: str
    worksheet_name: str


class CardService:
    def __init__(
        self, db: Database, sheets: SheetsClient, locks: LockManager, settings: Settings, auth: AuthorizationService
    ) -> None:
        self.db = db
        self.sheets = sheets
        self.locks = locks
        self.settings = settings
        self.auth = auth

    def _card_table(self, grid: SheetGrid, loader_number: int):
        return locate_card_table(
            grid, loader_number, self.settings.sheet_column_map, scan_rows=self.settings.sheet_header_scan_rows
        )

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
            n, entry_date = card.loader_number, card.entry_date
            trips = {int(k): v for k, v in (card.trips or {}).items()}
            fills = [Decimal(f) for f in card.hsd_fills or []]
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
                mapping = loaders.get_worksheet(loader.id, entry_date.year, entry_date.month)
                configured = mapping.worksheet_name if mapping and mapping.is_active else None
        if blocked:
            return self._set(card_id, CardStatus.BLOCKED, blocked.code, blocked.message)

        values: dict[str, Any] = {}
        try:
            info = self.sheets.get_spreadsheet_info(spreadsheet_id)
            resolution = resolve_worksheet(
                loader_number=n,
                year=entry_date.year,
                month=entry_date.month,
                configured_name=configured,
                available_titles=info.worksheet_titles,
                auto_detect=self.settings.worksheet_auto_detect,
            )
            values.update(spreadsheet_id=spreadsheet_id, worksheet_name=resolution.worksheet_name)
            grid = self.sheets.read_grid(spreadsheet_id, resolution.worksheet_name)
            table, layout, region = self._card_table(grid, n)
            row = find_date_row(table, layout, entry_date)
            values["sheet_row"] = row
            plan = plan_card(table, layout, row, trips, fills)
        except (SheetsError, WorksheetResolutionError, SheetLayoutError) as exc:
            return self._set(card_id, CardStatus.BLOCKED, exc.code, exc.message, **values)
        except CardBlocked as exc:
            return self._set(card_id, CardStatus.BLOCKED, exc.code, exc.message, **values)

        values["preview"] = {
            "cells": plan.cells,
            "day": plan.day,
            "changes": plan.changes,
            "table": region.title,
            "report_trips": self._report_trips(grid, n, entry_date, resolution.shared),
        }
        if not plan.writes:
            return self._set(card_id, CardStatus.UNCHANGED, "UNCHANGED", "The sheet already has these values — nothing to change", **values)
        if plan.changes:
            policy = self.settings.correction_policy
            if policy == CorrectionPolicy.REJECT:
                return self._set(
                    card_id, CardStatus.BLOCKED, "DUPLICATE_REJECTED", f"Already filled ({'; '.join(plan.changes)}); corrections are disabled", **values
                )
            if policy == CorrectionPolicy.ADMIN_APPROVAL and not submitter_is_admin:
                return self._set(
                    card_id, CardStatus.BLOCKED, "NEEDS_ADMIN", f"Changing filled cells ({'; '.join(plan.changes)}) needs an administrator", **values
                )
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
            if loader is None or not loader.is_active:
                card_status = (CardStatus.FAILED, "LOADER_DISABLED", "Loader was disabled before the update")
            elif loader.spreadsheet_id != card.spreadsheet_id:
                card_status = (CardStatus.CONFLICT, "CONFIGURATION_CHANGED", "The loader's spreadsheet changed; please send it again")
            else:
                card_status = None
                ctx = _Ctx(
                    card_id=card.id,
                    loader_number=card.loader_number,
                    entry_date=card.entry_date,
                    trips={int(k): v for k, v in (card.trips or {}).items()},
                    fills=[Decimal(f) for f in card.hsd_fills or []],
                    spreadsheet_id=card.spreadsheet_id,
                    worksheet_name=card.worksheet_name,
                )
                previewed = dict((card.preview or {}).get("cells") or {})
                previewed_row = card.sheet_row
        if card_status:
            return self._finish(card_id, *card_status)

        try:
            with self.locks.hold(f"sheet:{ctx.spreadsheet_id}:{ctx.worksheet_name}"):
                return self._apply_locked(ctx, previewed, previewed_row)
        except LockTimeout as exc:
            self._note_transient(card_id, str(exc))
            raise TransientApplyError(str(exc)) from exc
        except SheetsError as exc:
            if exc.retryable:
                self._note_transient(card_id, exc.message)
                raise TransientApplyError(exc.message) from exc
            return self._finish(card_id, CardStatus.FAILED, exc.code, exc.message)
        except SheetLayoutError as exc:
            return self._finish(card_id, CardStatus.FAILED, exc.code, exc.message)
        except CardBlocked as exc:
            return self._finish(card_id, CardStatus.FAILED, exc.code, exc.message)

    def _apply_locked(self, ctx: _Ctx, previewed: dict[str, dict], previewed_row: int) -> CardStatus:
        table, layout, _ = self._card_table(self.sheets.read_grid(ctx.spreadsheet_id, ctx.worksheet_name), ctx.loader_number)
        row = find_date_row(table, layout, ctx.entry_date)
        if row != previewed_row:
            return self._finish(
                ctx.card_id, CardStatus.CONFLICT, "ROW_MOVED", f"The row for {ctx.entry_date:%d-%m-%Y} moved from {previewed_row} to {row}; please send it again"
            )
        current = plan_card(table, layout, row, ctx.trips, ctx.fills).cells
        if set(current) != set(previewed):
            return self._finish(
                ctx.card_id, CardStatus.CONFLICT, "COLUMNS_MOVED", "The card block's columns changed since the preview; please send it again"
            )
        for ref, seen in previewed.items():
            now = current[ref]["before"]
            if now not in (seen["before"], seen["after"]):
                return self._finish(
                    ctx.card_id,
                    CardStatus.CONFLICT,
                    "SHEET_CHANGED",
                    f"{ref} changed since the preview (was {_show(seen['before'])}, now {_show(now)}); please send it again",
                )
        # Cells already at their planned value (a re-run after a crash) are not written again.
        to_write = {ref: seen for ref, seen in previewed.items() if current[ref]["before"] != seen["after"]}
        writes = [CellWrite(row, _col_of(ref), seen["write"]) for ref, seen in to_write.items()]

        with self.db.session() as session:
            card = CardRepository(session).get(ctx.card_id)
            if card.previous_values is None:
                card.previous_values = {ref: seen["before"] for ref, seen in previewed.items()}
            card.updated_values = {ref: seen["after"] for ref, seen in previewed.items()}
            card.status = CardStatus.APPLYING
            card.error_code = card.error_message = None

        self.sheets.write_cells(ctx.spreadsheet_id, ctx.worksheet_name, writes, user_entered=True)

        with self.db.session() as session:
            card = CardRepository(session).get(ctx.card_id)
            card.status = CardStatus.UPDATED
            card.applied_at = card.completed_at = utcnow()
            audit(
                session,
                "card_row_updated",
                actor=card.confirmed_by,
                entity_type="card_entry",
                entity_id=ctx.card_id,
                loader_number=ctx.loader_number,
                spreadsheet_id=ctx.spreadsheet_id,
                worksheet=ctx.worksheet_name,
                row=row,
                old=card.previous_values,
                new=card.updated_values,
            )
        log.info("card row updated", extra={"card_id": ctx.card_id, "worksheet": ctx.worksheet_name, "row": row})
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


def _col_of(ref: str) -> int:
    return column_index("".join(ch for ch in ref if ch.isalpha()))


def _show(value: Any) -> str:
    return "empty" if value is None else str(value)
