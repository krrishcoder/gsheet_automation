"""Business validation of a parsed report (totals cross-check, impossible figures, dates)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.schemas.report import IssueSeverity, LoaderEntry, ParsedReport, ValidationIssue


def _fmt_qty(value: Decimal) -> str:
    return f"{value:.3f}"


def validate_report(report: ParsedReport, *, today: date, tolerance_mt: Decimal) -> list[ValidationIssue]:
    """Return issues. ERROR issues with a loader_number block only that loader.

    Totals are compared against the sum of every equipment entry in the message (loaders and
    PC units), because the reported "Total Trip"/"Total Qty" lines cover the whole report.
    A mismatch is flagged, never "corrected".
    """
    issues: list[ValidationIssue] = []
    everything = [*report.loaders, *report.other_equipment]

    sum_trips = sum(e.trips for e in everything if e.trips is not None)
    sum_qty = sum((e.quantity_mt for e in everything if e.quantity_mt is not None), Decimal("0"))

    if report.reported_total_trips is not None and report.reported_total_trips != sum_trips:
        issues.append(
            ValidationIssue(
                code="TOTAL_TRIPS_MISMATCH",
                severity=IssueSeverity.WARNING,
                message=f"Reported total trips {report.reported_total_trips} ≠ sum of entries {sum_trips}",
            )
        )
    if report.reported_total_quantity_mt is not None and abs(report.reported_total_quantity_mt - sum_qty) > tolerance_mt:
        issues.append(
            ValidationIssue(
                code="TOTAL_QTY_MISMATCH",
                severity=IssueSeverity.WARNING,
                message=(
                    f"Reported total quantity {_fmt_qty(report.reported_total_quantity_mt)} MT ≠ "
                    f"sum of entries {_fmt_qty(sum_qty)} MT"
                ),
            )
        )

    if report.report_date > today + timedelta(days=1):
        issues.append(
            ValidationIssue(
                code="FUTURE_DATE",
                severity=IssueSeverity.WARNING,
                message=f"Report date {report.report_date:%d-%m-%Y} is in the future",
            )
        )

    for entry in report.loaders:
        issues.extend(validate_loader_entry(entry))
    return issues


def validate_loader_entry(entry: LoaderEntry) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    n = entry.loader_number
    if entry.error:
        issues.append(ValidationIssue(code="INVALID_VALUE", severity=IssueSeverity.ERROR, message=entry.error, loader_number=n))
    if entry.trips == 0 and entry.quantity_mt is not None and entry.quantity_mt > 0:
        issues.append(
            ValidationIssue(
                code="QTY_WITHOUT_TRIPS",
                severity=IssueSeverity.ERROR,
                message="Quantity reported with 0 trips",
                loader_number=n,
            )
        )
    if entry.trips and entry.quantity_mt is not None and entry.quantity_mt == 0:
        issues.append(
            ValidationIssue(
                code="TRIPS_WITHOUT_QTY",
                severity=IssueSeverity.WARNING,
                message=f"{entry.trips} trips reported with 0 MT",
                loader_number=n,
            )
        )
    if entry.has_data and (entry.trips is None) != (entry.quantity_mt is None):
        missing = "Trip" if entry.trips is None else "Qty"
        issues.append(
            ValidationIssue(
                code="PARTIAL_VALUES",
                severity=IssueSeverity.WARNING,
                message=f"{missing} is Nil/missing; only the reported value will be written",
                loader_number=n,
            )
        )
    return issues


def blocking_errors_for(issues: list[ValidationIssue], loader_number: int) -> list[ValidationIssue]:
    return [i for i in issues if i.severity == IssueSeverity.ERROR and i.loader_number == loader_number]
