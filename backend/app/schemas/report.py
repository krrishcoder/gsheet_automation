"""Validated structures produced by the WhatsApp report parser."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

SHIFT_LABELS = {1: "1st Shift", 2: "2nd Shift", 3: "3rd Shift"}


class _Figures(BaseModel):
    model_config = ConfigDict(frozen=True)

    trips: int | None = Field(default=None, ge=0)
    quantity_mt: Decimal | None = Field(default=None, ge=0)
    trips_nil: bool = False  # the report explicitly said "Nil" (unavailable, not zero)
    quantity_nil: bool = False
    error: str | None = None  # a value was present but unreadable

    @property
    def has_data(self) -> bool:
        return self.trips is not None or self.quantity_mt is not None


class LoaderEntry(_Figures):
    loader_number: int = Field(ge=1, le=999)

    @property
    def label(self) -> str:
        return f"Loader {self.loader_number:02d}"


class EquipmentEntry(_Figures):
    """Non-loader equipment found in the report (e.g. PC-15). Never routed to loader sheets."""

    category: str
    number: int = Field(ge=0, le=999)

    @property
    def label(self) -> str:
        return f"{self.category} {self.number:02d}"


class ParsedReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    report_date: date
    shift: int = Field(ge=1, le=3)
    loaders: list[LoaderEntry]
    other_equipment: list[EquipmentEntry] = Field(default_factory=list)
    reported_total_trips: int | None = Field(default=None, ge=0)
    reported_total_quantity_mt: Decimal | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @property
    def shift_label(self) -> str:
        return SHIFT_LABELS[self.shift]


class IssueSeverity(str, Enum):
    WARNING = "warning"  # shown in the summary, does not block anything
    ERROR = "error"  # blocks the affected loader (or the whole report when loader_number is None)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    severity: IssueSeverity
    message: str
    loader_number: int | None = None
