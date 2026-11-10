"""Per-loader preview (dry run against the live sheet) and apply (the actual write).

Each loader record is processed independently: one loader's failure never blocks another, and
results are stored per record. Spreadsheet writes for different loaders are NOT atomic as a group;
each record has its own durable state machine instead:

    PENDING -> READY | BLOCKED | SKIPPED                       (preview, before confirmation)
    READY -> QUEUED | AWAITING_APPROVAL | CANCELLED            (user confirms / cancels)
    QUEUED -> APPLYING -> UPDATED | FAILED | CONFLICT           (worker)

``APPLYING`` is persisted together with the row's previous values *before* the sheet is written,
so a crash mid-write is recovered by simply re-running the record: values are absolute (not
increments) and re-writing them is idempotent.

No database session is held open while Google APIs are called.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.core.config import CorrectionPolicy, Settings
from app.database.database import Database
from app.database.models import (
    LoaderReportRecord,
    OperationalReport,
    RecordOperation,
    RecordStatus,
    ReportStatus,
    utcnow,
)
from app.database.repositories import LoaderRepository, ReportRepository, ShiftEntryRepository, UserRepository, audit
from app.schemas.report import IssueSeverity, ValidationIssue
from app.services.authorization_service import AuthorizationService
from app.services.calculations import ShiftValues, compute_row_update
from app.schemas.sheets import SheetGrid
from app.services.google_sheets_service import SheetsClient, SheetsError, SpreadsheetInfo
from app.services.locks import LockManager, LockTimeout
from app.services.sheet_layout import (
    CellValueError,
    SheetLayout,
    SheetLayoutError,
    build_cell_writes,
    column_letter,
    find_date_row,
    locate_table,
    read_row_state,
    shift_columns,
)
from app.services.worksheet_service import WorksheetResolutionError, resolve_worksheet, title_matches

log = logging.getLogger(__name__)

FINAL_FAILURE_STATUSES = {RecordStatus.BLOCKED, RecordStatus.FAILED, RecordStatus.CONFLICT, RecordStatus.REJECTED}
IN_FLIGHT_STATUSES = {RecordStatus.QUEUED, RecordStatus.APPLYING}


class TransientApplyError(Exception):
    """A record could not be written for a temporary reason; the job should retry later."""


@dataclass
class _PreviewInput:
    record_id: int
    loader_number: int
    trips: int | None
    quantity: Decimal | None
    blocking: list[str]
    loader_id: int | None = None
    loader_active: bool = False
    spreadsheet_id: str | None = None
    configured_worksheet: str | None = None
    authorized: bool = False
    previously_submitted: ShiftValues | None = None


@dataclass
class PreviewOutcome:
    status: RecordStatus
    error_code: str | None = None
    message: str | None = None
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ApplyInput:
    record_id: int
    report_id: int
    loader_id: int
    loader_number: int
    report_date: date
    shift: int
    trips: int | None
    quantity: Decimal | None
    spreadsheet_id: str
    worksheet_name: str
    sheet_row: int
    preview_before: ShiftValues
    preview_planned: ShiftValues
    is_correction: bool
    actor: int | None
    shared_tab: bool = False


class ReportProcessor:
    def __init__(
        self,
        db: Database,
        sheets: SheetsClient,
        locks: LockManager,
        settings: Settings,
        auth: AuthorizationService,
    ) -> None:
        self.db = db
        self.sheets = sheets
        self.locks = locks
        self.settings = settings
        self.auth = auth

    # --- Sheet access helpers ---------------------------------------------------------------

    def _read_tab(self, spreadsheet_id: str, worksheet: str) -> SheetGrid:
        return self.sheets.read_grid(
            spreadsheet_id,
            worksheet,
            last_row=self.settings.sheet_read_max_rows,
            last_col=self.settings.sheet_read_last_column,
        )

    def _load_row(
        self, grid: SheetGrid, loader_number: int, report_date: date, *, require_title: bool = False
    ) -> tuple[SheetGrid, SheetLayout, int]:
        """This loader's table within the tab (a tab may hold several loaders' tables) and its date row.
        Everything after this works on the returned table grid only."""
        table, layout, _ = locate_table(
            grid,
            loader_number,
            self.settings.sheet_column_map,
            detection=self.settings.sheet_header_detection,
            strict=self.settings.sheet_header_strict,
            scan_rows=self.settings.sheet_header_scan_rows,
            require_title=require_title,
        )
        return table, layout, find_date_row(table, layout, report_date)

    # --- Preview ------------------------------------------------------------------------------

    def preview_report(self, report_id: int) -> OperationalReport:
        with self.db.session() as session:
            report = ReportRepository(session).get(report_id)
            if report is None:
                raise ValueError(f"report {report_id} not found")
            submitter = UserRepository(session).get_by_telegram_id(report.telegram_user_id)
            submitter_is_admin = self.auth.is_admin(submitter)
            issues = [ValidationIssue(**i) for i in report.validation_issues or []]
            report_date, shift = report.report_date, report.shift
            loaders = LoaderRepository(session)
            shift_entries = ShiftEntryRepository(session)
            inputs: list[_PreviewInput] = []
            for record in report.records:
                if record.processing_status != RecordStatus.PENDING:
                    continue
                inp = _PreviewInput(
                    record_id=record.id,
                    loader_number=record.loader_number,
                    trips=record.trips,
                    quantity=record.quantity_mt,
                    blocking=[
                        i.message
                        for i in issues
                        if i.severity == IssueSeverity.ERROR and i.loader_number == record.loader_number
                    ],
                )
                loader = loaders.get_by_number(record.loader_number)
                if loader is not None:
                    record.loader_id = loader.id
                    inp.loader_id = loader.id
                    inp.loader_active = loader.is_active
                    inp.spreadsheet_id = loader.spreadsheet_id
                    mapping = loaders.get_worksheet(loader.id, report_date.year, report_date.month)
                    inp.configured_worksheet = mapping.worksheet_name if mapping and mapping.is_active else None
                    inp.authorized = self.auth.can_submit_for(session, submitter, loader)
                    entry = shift_entries.get(loader.id, report_date, shift)
                    if entry is not None:
                        inp.previously_submitted = ShiftValues(entry.trips, entry.quantity_mt)
                inputs.append(inp)

        info_cache: dict[str, SpreadsheetInfo | SheetsError] = {}
        grid_cache: dict[tuple[str, str], SheetGrid] = {}  # one read per tab, even if it holds several loaders
        outcomes = {
            inp.record_id: self._preview_one(inp, report_date, shift, submitter_is_admin, info_cache, grid_cache)
            for inp in inputs
        }
        self._hint_unregistered_tabs(inputs, outcomes, info_cache, report_date)

        with self.db.session() as session:
            repo = ReportRepository(session)
            report = repo.get(report_id)
            assert report is not None
            for record in report.records:
                outcome = outcomes.get(record.id)
                if outcome is None:
                    continue
                record.processing_status = outcome.status
                record.error_code = outcome.error_code
                record.error_message = outcome.message
                for key, value in outcome.values.items():
                    setattr(record, key, value)
            ready = [r for r in report.records if r.processing_status == RecordStatus.READY]
            report.status = ReportStatus.PENDING_CONFIRMATION if ready else ReportStatus.REJECTED
            if not ready:
                report.completed_at = utcnow()
            return report

    @staticmethod
    def _hint_unregistered_tabs(
        inputs: list[_PreviewInput],
        outcomes: dict[int, PreviewOutcome],
        info_cache: dict[str, SpreadsheetInfo | SheetsError],
        report_date: date,
    ) -> None:
        """An unregistered loader whose tab sits in a spreadsheet this report already opened (one file,
        many loader tabs) gets a pointer to it. Only a hint: nothing is written to an unregistered loader."""
        owners: dict[str, int] = {}
        for inp in inputs:
            if inp.spreadsheet_id and inp.spreadsheet_id not in owners:
                owners[inp.spreadsheet_id] = inp.loader_number
        for inp in inputs:
            outcome = outcomes[inp.record_id]
            if outcome.error_code != "LOADER_NOT_CONFIGURED":
                continue
            n = inp.loader_number
            for spreadsheet_id, info in info_cache.items():
                if not isinstance(info, SpreadsheetInfo):
                    continue
                tabs = [
                    t for t in info.worksheet_titles
                    if title_matches(t, loader_number=n, year=report_date.year, month=report_date.month)
                ]
                if tabs:
                    owner = owners.get(spreadsheet_id)
                    outcome.message = (
                        f"Loader {n:02d} is not registered, but tab '{tabs[0]}' is in the spreadsheet of "
                        f"Loader {owner:02d} (admin: /set_sheet {n} <that link>, or /set_sheet <that link> "
                        "to register every loader tab in it)"
                    )
                    break

    def _spreadsheet_info(self, spreadsheet_id: str, cache: dict[str, SpreadsheetInfo | SheetsError]) -> SpreadsheetInfo:
        if spreadsheet_id not in cache:
            try:
                cache[spreadsheet_id] = self.sheets.get_spreadsheet_info(spreadsheet_id)
            except SheetsError as exc:
                cache[spreadsheet_id] = exc
        cached = cache[spreadsheet_id]
        if isinstance(cached, SheetsError):
            raise cached
        return cached

    def _preview_one(
        self,
        inp: _PreviewInput,
        report_date: date,
        shift: int,
        submitter_is_admin: bool,
        info_cache: dict[str, SpreadsheetInfo | SheetsError],
        grid_cache: dict[tuple[str, str], SheetGrid],
    ) -> PreviewOutcome:
        n = inp.loader_number
        if inp.blocking:
            return PreviewOutcome(RecordStatus.BLOCKED, "INVALID_DATA", "; ".join(inp.blocking))
        if inp.trips is None and inp.quantity is None:
            return PreviewOutcome(RecordStatus.SKIPPED, "NO_DATA", "Nil / no values — nothing written")
        if inp.loader_id is None:
            return PreviewOutcome(
                RecordStatus.BLOCKED,
                "LOADER_NOT_CONFIGURED",
                f"No spreadsheet configured for Loader {n:02d} (admin: /set_sheet {n} <URL>)",
            )
        if not inp.loader_active:
            return PreviewOutcome(RecordStatus.BLOCKED, "LOADER_DISABLED", f"Loader {n:02d} is disabled")
        if not inp.authorized:
            return PreviewOutcome(RecordStatus.BLOCKED, "UNAUTHORIZED", f"You are not authorised for Loader {n:02d}")

        assert inp.spreadsheet_id is not None
        try:
            info = self._spreadsheet_info(inp.spreadsheet_id, info_cache)
            resolution = resolve_worksheet(
                loader_number=n,
                year=report_date.year,
                month=report_date.month,
                configured_name=inp.configured_worksheet,
                available_titles=info.worksheet_titles,
                auto_detect=self.settings.worksheet_auto_detect,
            )
            key = (inp.spreadsheet_id, resolution.worksheet_name)
            if key not in grid_cache:
                grid_cache[key] = self._read_tab(*key)
            grid, layout, row = self._load_row(grid_cache[key], n, report_date, require_title=resolution.shared)
            state = read_row_state(grid, layout, row)
        except (SheetsError, WorksheetResolutionError, SheetLayoutError) as exc:
            return PreviewOutcome(RecordStatus.BLOCKED, exc.code, exc.message)
        except CellValueError as exc:
            return PreviewOutcome(RecordStatus.BLOCKED, "CELL_VALUE_INVALID", str(exc))

        trips_col, weight_col, _ = shift_columns(shift)
        locked = [c for c, v in ((trips_col, inp.trips), (weight_col, inp.quantity)) if v is not None and c in state.formulas]
        if locked:
            cells = ", ".join(f"{layout.letter(c)}{row}" for c in locked)
            return PreviewOutcome(RecordStatus.BLOCKED, "TARGET_HAS_FORMULA", f"Cell {cells} contains a formula; not overwriting it")

        computation = compute_row_update(
            state.shifts,
            shift,
            inp.trips,
            inp.quantity,
            weight_decimals=self.settings.weight_decimals,
            average_decimals=self.settings.average_decimals,
        )
        values: dict[str, Any] = {
            "spreadsheet_id": inp.spreadsheet_id,
            "worksheet_name": resolution.worksheet_name,
            "worksheet_auto_detected": resolution.auto_detected,
            "sheet_row": row,
            "preview_values": {
                "before": computation.before.to_dict(),
                "planned": computation.after.to_dict(),
                "shared_tab": resolution.shared,
            },
        }
        if not computation.changed:
            return PreviewOutcome(
                RecordStatus.SKIPPED, "UNCHANGED", "Same values are already in the sheet — nothing to change", values
            )

        is_correction = not computation.before.is_empty or inp.previously_submitted is not None
        values["is_correction"] = is_correction
        values["operation"] = RecordOperation.REPLACE if not computation.before.is_empty else RecordOperation.NEW
        if is_correction:
            if self.settings.correction_policy == CorrectionPolicy.REJECT:
                return PreviewOutcome(
                    RecordStatus.BLOCKED,
                    "DUPLICATE_REJECTED",
                    f"{_shift_name(shift)} already has data ({_fmt_shift(computation.before)}); corrections are disabled",
                    values,
                )
            values["requires_approval"] = (
                self.settings.correction_policy == CorrectionPolicy.ADMIN_APPROVAL and not submitter_is_admin
            )
        return PreviewOutcome(RecordStatus.READY, values=values)

    # --- Apply ----------------------------------------------------------------------------------

    def apply_record(self, record_id: int) -> RecordStatus | None:
        """Write one confirmed record. Raises TransientApplyError when it should be retried later."""
        with self.db.session() as session:
            record = session.get(LoaderReportRecord, record_id)
            if record is None or record.processing_status not in IN_FLIGHT_STATUSES:
                return None
            report = record.report
            loader = record.loader
            record.attempts += 1
            if loader is None or not loader.is_active:
                self._mark(session, record, RecordStatus.FAILED, "LOADER_DISABLED", "Loader was disabled before the update")
                return RecordStatus.FAILED
            if loader.spreadsheet_id != record.spreadsheet_id:
                self._mark(
                    session,
                    record,
                    RecordStatus.CONFLICT,
                    "CONFIGURATION_CHANGED",
                    "The loader's spreadsheet was changed after the summary was shown; please resend the report",
                )
                return RecordStatus.CONFLICT
            preview = record.preview_values or {}
            inp = _ApplyInput(
                record_id=record.id,
                report_id=report.id,
                loader_id=loader.id,
                loader_number=record.loader_number,
                report_date=report.report_date,
                shift=report.shift,
                trips=record.trips,
                quantity=record.quantity_mt,
                spreadsheet_id=record.spreadsheet_id,
                worksheet_name=record.worksheet_name,
                sheet_row=record.sheet_row,
                preview_before=ShiftValues.from_dict(preview.get("before")),
                preview_planned=ShiftValues.from_dict(preview.get("planned")),
                is_correction=record.is_correction,
                shared_tab=bool(preview.get("shared_tab")),
                actor=report.confirmed_by,
            )

        lock_name = f"sheet:{inp.spreadsheet_id}:{inp.worksheet_name}"
        try:
            with self.locks.hold(lock_name):
                return self._apply_locked(inp)
        except LockTimeout as exc:
            self._note_transient(inp.record_id, str(exc))
            raise TransientApplyError(str(exc)) from exc
        except SheetsError as exc:
            if exc.retryable:
                self._note_transient(inp.record_id, exc.message)
                raise TransientApplyError(exc.message) from exc
            return self._finish_with(inp.record_id, RecordStatus.FAILED, exc.code, exc.message)
        except SheetLayoutError as exc:
            return self._finish_with(inp.record_id, RecordStatus.FAILED, exc.code, exc.message)
        except CellValueError as exc:
            return self._finish_with(inp.record_id, RecordStatus.FAILED, "CELL_VALUE_INVALID", str(exc))

    def _apply_locked(self, inp: _ApplyInput) -> RecordStatus:
        grid, layout, row = self._load_row(
            self._read_tab(inp.spreadsheet_id, inp.worksheet_name),
            inp.loader_number,
            inp.report_date,
            require_title=inp.shared_tab,
        )
        if row != inp.sheet_row:
            return self._finish_with(
                inp.record_id,
                RecordStatus.CONFLICT,
                "ROW_MOVED",
                f"The row for {inp.report_date:%d-%m-%Y} moved from {inp.sheet_row} to {row}; please resend the report",
            )
        state = read_row_state(grid, layout, row)
        current = state.shifts[inp.shift]
        if current not in (inp.preview_before, inp.preview_planned):
            return self._finish_with(
                inp.record_id,
                RecordStatus.CONFLICT,
                "SHEET_CHANGED",
                f"{_shift_name(inp.shift)} values changed since the summary "
                f"(was {_fmt_shift(inp.preview_before)}, now {_fmt_shift(current)}); please resend the report",
            )
        trips_col, weight_col, _ = shift_columns(inp.shift)
        if (inp.trips is not None and trips_col in state.formulas) or (inp.quantity is not None and weight_col in state.formulas):
            return self._finish_with(
                inp.record_id, RecordStatus.FAILED, "TARGET_HAS_FORMULA", "Target cell now contains a formula"
            )

        computation = compute_row_update(
            state.shifts,
            inp.shift,
            inp.trips,
            inp.quantity,
            weight_decimals=self.settings.weight_decimals,
            average_decimals=self.settings.average_decimals,
        )
        writes = build_cell_writes(
            layout,
            row,
            computation,
            reported_trips=inp.trips,
            reported_weight=inp.quantity,
            formulas=state.formulas,
        )
        updated_values = {
            "shift": inp.shift,
            "shift_values": computation.after.to_dict(),
            "average": _s(computation.average),
            "day_total_weight": _s(computation.day_total_weight),
            "day_total_trips": computation.day_total_trips,
            "cells": {f"{column_letter(w.col)}{w.row}": w.value for w in writes},
            "formula_cells_kept": sorted(f"{layout.letter(c)}{row}" for c in state.formulas),
        }

        # Durable intent before touching the sheet (crash recovery: see module docstring).
        with self.db.session() as session:
            record = session.get(LoaderReportRecord, inp.record_id)
            if record.previous_values is None:
                record.previous_values = state.snapshot()
            record.updated_values = updated_values
            record.processing_status = RecordStatus.APPLYING
            record.error_code = record.error_message = None

        self.sheets.write_cells(inp.spreadsheet_id, inp.worksheet_name, writes)

        with self.db.session() as session:
            record = session.get(LoaderReportRecord, inp.record_id)
            record.processing_status = RecordStatus.UPDATED
            record.operation = RecordOperation.NEW if inp.preview_before.is_empty else RecordOperation.REPLACE
            record.applied_at = utcnow()
            ShiftEntryRepository(session).upsert(
                loader_id=inp.loader_id,
                report_date=inp.report_date,
                shift=inp.shift,
                trips=computation.after.trips,
                quantity_mt=computation.after.weight,
                record_id=inp.record_id,
            )
            audit(
                session,
                "sheet_row_updated",
                actor=inp.actor,
                entity_type="loader_report_record",
                entity_id=inp.record_id,
                report_id=inp.report_id,
                loader_number=inp.loader_number,
                spreadsheet_id=inp.spreadsheet_id,
                worksheet=inp.worksheet_name,
                row=row,
                old=record.previous_values,
                new=updated_values,
            )
        log.info(
            "sheet row updated",
            extra={
                "record_id": inp.record_id,
                "report_id": inp.report_id,
                "loader_number": inp.loader_number,
                "worksheet": inp.worksheet_name,
                "row": row,
            },
        )
        return RecordStatus.UPDATED

    # --- State helpers ----------------------------------------------------------------------------

    @staticmethod
    def _mark(session, record: LoaderReportRecord, status: RecordStatus, code: str | None, message: str | None) -> None:
        record.processing_status = status
        record.error_code = code
        record.error_message = message

    def _finish_with(self, record_id: int, status: RecordStatus, code: str, message: str) -> RecordStatus:
        with self.db.session() as session:
            record = session.get(LoaderReportRecord, record_id)
            self._mark(session, record, status, code, message)
            audit(
                session,
                "sheet_update_not_applied",
                actor=None,
                entity_type="loader_report_record",
                entity_id=record_id,
                status=status,
                code=code,
                message=message,
            )
        log.warning("record not applied", extra={"record_id": record_id, "status": status.value, "code": code})
        return status

    def _note_transient(self, record_id: int, message: str) -> None:
        with self.db.session() as session:
            record = session.get(LoaderReportRecord, record_id)
            record.error_code = "TEMPORARY_ERROR"
            record.error_message = f"Temporary error, will retry: {message}"

    def fail_in_flight(self, record_ids: list[int], message: str) -> None:
        """Give up on records that kept failing transiently (job attempts exhausted)."""
        with self.db.session() as session:
            for record_id in record_ids:
                record = session.get(LoaderReportRecord, record_id)
                if record is not None and record.processing_status in IN_FLIGHT_STATUSES:
                    self._mark(session, record, RecordStatus.FAILED, "SHEETS_UNAVAILABLE", message)

    def finalize_report(self, report_id: int) -> ReportStatus:
        with self.db.session() as session:
            report = ReportRepository(session).get(report_id)
            assert report is not None
            statuses = [r.processing_status for r in report.records]
            if any(s in IN_FLIGHT_STATUSES for s in statuses):
                return report.status
            if report.status in (ReportStatus.CANCELLED, ReportStatus.EXPIRED, ReportStatus.REJECTED):
                return report.status
            updated = sum(s == RecordStatus.UPDATED for s in statuses)
            failed = sum(s in FINAL_FAILURE_STATUSES for s in statuses)
            if any(s == RecordStatus.AWAITING_APPROVAL for s in statuses):
                status = ReportStatus.AWAITING_APPROVAL
            elif failed == 0:
                status = ReportStatus.COMPLETED
            elif updated:
                status = ReportStatus.PARTIALLY_COMPLETED
            else:
                status = ReportStatus.FAILED
            report.status = status
            if status != ReportStatus.AWAITING_APPROVAL:
                report.completed_at = utcnow()
            return status


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def _shift_name(shift: int) -> str:
    return {1: "1st Shift", 2: "2nd Shift", 3: "3rd Shift"}[shift]


def _fmt_shift(values: ShiftValues) -> str:
    if values.is_empty:
        return "empty"
    trips = "–" if values.trips is None else str(values.trips)
    weight = "–" if values.weight is None else f"{values.weight:.3f}"
    return f"{trips} trips / {weight} MT"
