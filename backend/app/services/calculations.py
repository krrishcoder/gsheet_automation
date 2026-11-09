"""Decimal arithmetic for shift averages and daily totals.

Rules:
* a value missing from the report (absent or "Nil") never overwrites what the sheet holds;
* a reported 0 is a real zero;
* daily totals are recomputed from all three shifts as they stand after the update.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class ShiftValues:
    trips: int | None = None
    weight: Decimal | None = None

    @property
    def is_empty(self) -> bool:
        return self.trips is None and self.weight is None

    def to_dict(self) -> dict[str, str | int | None]:
        return {"trips": self.trips, "weight": None if self.weight is None else str(self.weight)}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ShiftValues":
        if not data:
            return cls()
        weight = data.get("weight")
        return cls(trips=data.get("trips"), weight=None if weight is None else Decimal(str(weight)))


@dataclass(frozen=True)
class RowComputation:
    shift: int
    before: ShiftValues
    after: ShiftValues
    average: Decimal | None
    day_total_weight: Decimal | None
    day_total_trips: int | None
    shifts_after: dict[int, ShiftValues]

    @property
    def changed(self) -> bool:
        return self.before != self.after


def quantize(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def merge_shift(existing: ShiftValues, trips: int | None, weight: Decimal | None, *, weight_decimals: int = 3) -> ShiftValues:
    return ShiftValues(
        trips=trips if trips is not None else existing.trips,
        weight=quantize(weight, weight_decimals) if weight is not None else existing.weight,
    )


def shift_average(values: ShiftValues, *, decimals: int = 2) -> Decimal | None:
    """Weight / trips, rounded half-up. Undefined (None) when either side is missing or trips is 0
    with a non-zero weight; 0 trips with 0 weight averages to 0."""
    if values.trips is None or values.weight is None:
        return None
    if values.trips == 0:
        return quantize(Decimal(0), decimals) if values.weight == 0 else None
    return quantize(values.weight / Decimal(values.trips), decimals)


def day_totals(shifts: dict[int, ShiftValues], *, weight_decimals: int = 3) -> tuple[Decimal | None, int | None]:
    weights = [s.weight for s in shifts.values() if s.weight is not None]
    trips = [s.trips for s in shifts.values() if s.trips is not None]
    total_weight = quantize(sum(weights, Decimal(0)), weight_decimals) if weights else None
    total_trips = sum(trips) if trips else None
    return total_weight, total_trips


def compute_row_update(
    existing: dict[int, ShiftValues],
    shift: int,
    trips: int | None,
    weight: Decimal | None,
    *,
    weight_decimals: int = 3,
    average_decimals: int = 2,
) -> RowComputation:
    if shift not in (1, 2, 3):
        raise ValueError(f"invalid shift {shift}")
    before = existing.get(shift, ShiftValues())
    after = merge_shift(before, trips, weight, weight_decimals=weight_decimals)
    shifts_after = {n: existing.get(n, ShiftValues()) for n in (1, 2, 3)}
    shifts_after[shift] = after
    total_weight, total_trips = day_totals(shifts_after, weight_decimals=weight_decimals)
    return RowComputation(
        shift=shift,
        before=before,
        after=after,
        average=shift_average(after, decimals=average_decimals),
        day_total_weight=total_weight,
        day_total_trips=total_trips,
        shifts_after=shifts_after,
    )
