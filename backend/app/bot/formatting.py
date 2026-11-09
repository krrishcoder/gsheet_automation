"""Telegram message rendering (HTML parse mode)."""

from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Any

from app.database.models import LoaderReportRecord, OperationalReport, RecordOperation, RecordStatus, ReportStatus
from app.schemas.report import SHIFT_LABELS, IssueSeverity, ValidationIssue
from app.services.calculations import ShiftValues

RECORD_ICONS = {
    RecordStatus.PENDING: "…",
    RecordStatus.READY: "✅",
    RecordStatus.BLOCKED: "⛔",
    RecordStatus.SKIPPED: "➖",
    RecordStatus.AWAITING_APPROVAL: "⏳",
    RecordStatus.QUEUED: "⏳",
    RecordStatus.APPLYING: "⏳",
    RecordStatus.UPDATED: "✅",
    RecordStatus.FAILED: "❌",
    RecordStatus.CONFLICT: "⚠️",
    RecordStatus.CANCELLED: "✖️",
    RecordStatus.REJECTED: "⛔",
}

REPORT_HEADLINES = {
    ReportStatus.COMPLETED: "✅ SUCCESS",
    ReportStatus.PARTIALLY_COMPLETED: "⚠️ PARTIAL SUCCESS",
    ReportStatus.FAILED: "❌ FAILED",
    ReportStatus.AWAITING_APPROVAL: "⏳ WAITING FOR ADMIN APPROVAL",
    ReportStatus.CONFIRMED: "⏳ PROCESSING",
    ReportStatus.CANCELLED: "✖️ CANCELLED",
    ReportStatus.EXPIRED: "⌛ EXPIRED",
    ReportStatus.REJECTED: "⛔ NOTHING TO UPDATE",
    ReportStatus.PENDING_CONFIRMATION: "❔ WAITING FOR CONFIRMATION",
    ReportStatus.PREVIEWING: "… CHECKING",
}


def h(value: Any) -> str:
    return escape(str(value), quote=False)


def fmt_qty(value: Decimal | str | None) -> str:
    if value is None:
        return "Nil"
    return f"{Decimal(str(value)):.3f}"


def fmt_trips(value: int | None) -> str:
    return "Nil" if value is None else str(value)


def fmt_date(value) -> str:
    return value.strftime("%d-%m-%Y")


def fmt_shift_values(values: ShiftValues) -> str:
    """Sheet-side values: a blank cell is shown as "–" (Nil is a report term)."""
    if values.is_empty:
        return "empty"
    trips = "–" if values.trips is None else str(values.trips)
    weight = "–" if values.weight is None else fmt_qty(values.weight)
    return f"{trips} trips / {weight} MT"


def loader_label(number: int) -> str:
    return f"Loader {number:02d}"


def _figures(record: LoaderReportRecord) -> str:
    if record.trips is None and record.quantity_mt is None:
        return "Nil"
    return f"{fmt_trips(record.trips)} trips · {fmt_qty(record.quantity_mt)} MT"


def _where(record: LoaderReportRecord) -> str:
    if not record.worksheet_name:
        return ""
    auto = " (tab auto-selected)" if record.worksheet_auto_detected else ""
    return f"'{h(record.worksheet_name)}'{auto} · row {record.sheet_row}"


def _preview_line(record: LoaderReportRecord) -> str:
    status = record.processing_status
    icon = RECORD_ICONS[status]
    before = ShiftValues.from_dict((record.preview_values or {}).get("before"))
    if status == RecordStatus.READY:
        if record.is_correction and not before.is_empty:
            action = f"<b>will REPLACE</b> existing {fmt_shift_values(before)}"
            icon = "⚠️"
        elif record.is_correction:
            action = "re-submission (sheet cells are empty)"
            icon = "⚠️"
        else:
            action = "new entry"
        approval = "\n   ⏳ needs administrator approval after you confirm" if record.requires_approval else ""
        return f"   {icon} {action} → {_where(record)}{approval}"
    return f"   {icon} {h(record.error_message or status.value)}"


def render_issues(issues: list[ValidationIssue]) -> list[str]:
    lines = []
    for issue in issues:
        if issue.loader_number is not None and issue.severity == IssueSeverity.ERROR:
            continue  # shown on the loader's own line
        prefix = f"{loader_label(issue.loader_number)}: " if issue.loader_number is not None else ""
        lines.append(f"⚠️ {prefix}{h(issue.message)}")
    return lines


def render_summary(report: OperationalReport, *, ttl_minutes: int) -> str:
    parsed = report.parsed_data or {}
    issues = [ValidationIssue(**i) for i in report.validation_issues or []]
    lines = [
        f"📋 <b>Report received</b> (#{report.id})",
        f"Date: <b>{fmt_date(report.report_date)}</b>",
        f"Shift: <b>{SHIFT_LABELS[report.shift]}</b>",
        "",
        "<b>Extracted loaders:</b>",
    ]
    for record in report.records:
        lines.append(f"• <b>{loader_label(record.loader_number)}</b>: {_figures(record)}")
        lines.append(_preview_line(record))
    if not report.records:
        lines.append("(none)")

    others = parsed.get("other_equipment") or []
    if others:
        parts = []
        for o in others:
            figures = "Nil" if o.get("trips") is None and o.get("quantity_mt") is None else (
                f"{fmt_trips(o.get('trips'))} trips · {fmt_qty(o.get('quantity_mt'))} MT"
            )
            parts.append(f"{h(o['category'])} {int(o['number']):02d} {figures}")
        lines += ["", "<b>Not routed</b> (not loaders): " + "; ".join(parts)]

    total_trips = parsed.get("reported_total_trips")
    total_qty = parsed.get("reported_total_quantity_mt")
    if total_trips is not None or total_qty is not None:
        mismatch = any(i.code.startswith("TOTAL_") for i in issues)
        lines += [
            "",
            f"<b>Reported totals:</b> {fmt_trips(total_trips)} trips · {fmt_qty(total_qty)} MT"
            + ("" if mismatch else " ✅ match"),
        ]
    warnings = render_issues(issues) + [f"⚠️ {h(w)}" for w in parsed.get("warnings") or []]
    if warnings:
        lines += [""] + warnings

    ready = [r for r in report.records if r.processing_status == RecordStatus.READY]
    lines.append("")
    if report.status == ReportStatus.PENDING_CONFIRMATION:
        lines.append(
            f"Press <b>Confirm Update</b> to write {len(ready)} loader sheet(s), or <b>Cancel</b>. "
            f"(Expires in {ttl_minutes} min.)"
        )
    elif report.status == ReportStatus.REJECTED:
        lines.append("⛔ Nothing can be written from this report.")
    return "\n".join(lines)


def _result_line(record: LoaderReportRecord) -> str:
    label = f"<b>{loader_label(record.loader_number)}</b>"
    status = record.processing_status
    icon = RECORD_ICONS[status]
    if status == RecordStatus.UPDATED:
        before = ShiftValues.from_dict((record.preview_values or {}).get("before"))
        op = "replaced " + fmt_shift_values(before) if record.operation == RecordOperation.REPLACE else "new entry"
        updated = record.updated_values or {}
        after = ShiftValues.from_dict(updated.get("shift_values"))
        avg = updated.get("average")
        detail = f"{fmt_shift_values(after)}" + (f" · avg {avg}" if avg is not None else "")
        totals = f"day total {fmt_trips(updated.get('day_total_trips'))} trips / {fmt_qty(updated.get('day_total_weight'))} MT"
        return (
            f"{icon} {label}: Updated successfully ({op})\n"
            f"   {_where(record)} · {detail}\n   {totals}"
        )
    if status == RecordStatus.SKIPPED:
        return f"{icon} {label}: Skipped — {h(record.error_message)}"
    if status in (RecordStatus.AWAITING_APPROVAL, RecordStatus.QUEUED, RecordStatus.APPLYING):
        text = {
            RecordStatus.AWAITING_APPROVAL: "waiting for administrator approval",
            RecordStatus.QUEUED: "queued",
            RecordStatus.APPLYING: "in progress",
        }[status]
        return f"{icon} {label}: {text}"
    if status == RecordStatus.CANCELLED:
        return f"{icon} {label}: Cancelled"
    return f"{icon} {label}: NOT updated — {h(record.error_message or status.value)}"


def render_result(report: OperationalReport, records: list[LoaderReportRecord] | None = None, *, title: str | None = None) -> str:
    records = report.records if records is None else records
    with_data = [r for r in report.records if not (r.processing_status == RecordStatus.SKIPPED and r.error_code == "NO_DATA")]
    updated = sum(r.processing_status == RecordStatus.UPDATED for r in with_data)
    lines = [
        f"{REPORT_HEADLINES.get(report.status, report.status.value)} — <b>Report #{report.id}</b>",
        f"{fmt_date(report.report_date)} · {SHIFT_LABELS[report.shift]}",
    ]
    if title:
        lines.append(h(title))
    lines.append("")
    lines += [_result_line(r) for r in records]
    lines += ["", f"Updated {updated} of {len(with_data)} loader(s) with data."]
    return "\n".join(lines)


def render_report_brief(report: OperationalReport) -> str:
    counts: dict[str, int] = {}
    for r in report.records:
        counts[r.processing_status.value] = counts.get(r.processing_status.value, 0) + 1
    detail = ", ".join(f"{k.lower()} {v}" for k, v in sorted(counts.items()))
    return (
        f"#{report.id} · {fmt_date(report.report_date)} · {SHIFT_LABELS[report.shift]} · "
        f"<b>{report.status.value}</b> ({detail}) · by <code>{report.telegram_user_id}</code>"
    )
