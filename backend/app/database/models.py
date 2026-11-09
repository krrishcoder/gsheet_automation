"""SQLAlchemy ORM models (SQLite by default, PostgreSQL compatible)."""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Stores naive UTC, always returns timezone-aware UTC (SQLite drops tzinfo otherwise)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


class ExactDecimal(TypeDecorator):
    """Exact decimals: NUMERIC on PostgreSQL, decimal strings on SQLite (no float rounding)."""

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int = 14, scale: int = 3) -> None:
        super().__init__()
        self.precision = precision
        self.scale = scale

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(Numeric(self.precision, self.scale))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = Decimal(value)
        return str(value) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(str(value))


def _enum(enum_cls: type[enum.Enum]) -> Enum:
    return Enum(enum_cls, native_enum=False, length=32, validate_strings=True)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, datetime: UTCDateTime}


# --- Enumerations ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class UpdateStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    FAILED = "FAILED"


class ReportStatus(str, enum.Enum):
    PREVIEWING = "PREVIEWING"  # extracted, checking sheets
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"  # summary sent, waiting for Confirm/Cancel
    REJECTED = "REJECTED"  # nothing in the report can be applied
    CONFIRMED = "CONFIRMED"  # user confirmed, sheet updates queued
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # some corrections wait for an administrator
    COMPLETED = "COMPLETED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class RecordStatus(str, enum.Enum):
    PENDING = "PENDING"  # not yet checked against the sheet
    READY = "READY"  # checked; will be written when the report is confirmed
    BLOCKED = "BLOCKED"  # cannot be written (see error_code); final
    SKIPPED = "SKIPPED"  # nothing to write (Nil values or identical data); final
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    QUEUED = "QUEUED"  # confirmed, waiting for the worker
    APPLYING = "APPLYING"  # previous values captured, sheet write in progress
    UPDATED = "UPDATED"  # final
    FAILED = "FAILED"  # write failed; an admin may /retry_report
    CONFLICT = "CONFLICT"  # sheet changed between preview and write; final
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"  # correction rejected by an administrator


class RecordOperation(str, enum.Enum):
    NEW = "NEW"  # shift cells were empty
    REPLACE = "REPLACE"  # existing shift values were replaced (a correction)


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    DEAD = "DEAD"  # gave up after max attempts


# --- Access control -------------------------------------------------------------------------

user_loaders = Table(
    "user_loaders",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("loader_id", ForeignKey("loaders.id", ondelete="CASCADE"), primary_key=True),
    Column("granted_by", BigInteger, nullable=True),
    Column("granted_at", UTCDateTime, nullable=False, default=utcnow),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    full_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.USER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    loaders: Mapped[list["Loader"]] = relationship(
        secondary=user_loaders, back_populates="users", order_by="Loader.loader_number"
    )

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.full_name or str(self.telegram_user_id)


class Loader(Base):
    __tablename__ = "loaders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loader_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    spreadsheet_url: Mapped[str] = mapped_column(Text)
    spreadsheet_id: Mapped[str] = mapped_column(String(128), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    worksheets: Mapped[list["LoaderWorksheet"]] = relationship(
        back_populates="loader",
        cascade="all, delete-orphan",
        order_by=lambda: [LoaderWorksheet.year, LoaderWorksheet.month],
    )
    users: Mapped[list[User]] = relationship(secondary=user_loaders, back_populates="loaders")

    __table_args__ = (CheckConstraint("loader_number > 0", name="ck_loader_number_positive"),)

    @property
    def label(self) -> str:
        return f"Loader {self.loader_number:02d}"


class LoaderWorksheet(Base):
    __tablename__ = "loader_worksheets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loader_id: Mapped[int] = mapped_column(ForeignKey("loaders.id", ondelete="CASCADE"), index=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    worksheet_name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    loader: Mapped[Loader] = relationship(back_populates="worksheets")

    __table_args__ = (
        UniqueConstraint("loader_id", "year", "month", name="uq_loader_worksheet_month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_worksheet_month"),
        CheckConstraint("year BETWEEN 2000 AND 2100", name="ck_worksheet_year"),
    )


class TrustedChat(Base):
    """Group chats whose members may submit reports (when ALLOW_GROUP_SUBMISSIONS is on)."""

    __tablename__ = "trusted_chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# --- Telegram updates & reports -------------------------------------------------------------


class TelegramUpdate(Base):
    """Every accepted webhook delivery. ``update_id`` as primary key gives delivery idempotency."""

    __tablename__ = "telegram_updates"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    update_type: Mapped[str] = mapped_column(String(32))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    processing_status: Mapped[UpdateStatus] = mapped_column(_enum(UpdateStatus), default=UpdateStatus.RECEIVED)
    error_message: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_telegram_updates_status", "processing_status"),)


class OperationalReport(Base):
    __tablename__ = "operational_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_date: Mapped[date] = mapped_column(Date)
    shift: Mapped[int] = mapped_column(Integer)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    source_update_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("telegram_updates.update_id"), unique=True
    )
    raw_text: Mapped[str] = mapped_column(Text)
    parsed_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    validation_issues: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[ReportStatus] = mapped_column(_enum(ReportStatus), default=ReportStatus.PREVIEWING)
    summary_message_id: Mapped[int | None] = mapped_column(BigInteger)
    result_message_id: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    records: Mapped[list["LoaderReportRecord"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="LoaderReportRecord.loader_number"
    )

    __table_args__ = (
        CheckConstraint("shift BETWEEN 1 AND 3", name="ck_report_shift"),
        Index("ix_reports_date_shift", "report_date", "shift"),
        Index("ix_reports_status", "status"),
    )


class LoaderReportRecord(Base):
    """One loader's figures from one report, with its own processing state and audit values."""

    __tablename__ = "loader_report_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("operational_reports.id", ondelete="CASCADE"), index=True)
    loader_id: Mapped[int | None] = mapped_column(ForeignKey("loaders.id"), index=True)
    loader_number: Mapped[int] = mapped_column(Integer)
    trips: Mapped[int | None] = mapped_column(Integer)
    quantity_mt: Mapped[Decimal | None] = mapped_column(ExactDecimal(14, 3))
    processing_status: Mapped[RecordStatus] = mapped_column(_enum(RecordStatus), default=RecordStatus.PENDING)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    operation: Mapped[RecordOperation | None] = mapped_column(_enum(RecordOperation))
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    spreadsheet_id: Mapped[str | None] = mapped_column(String(128))
    worksheet_name: Mapped[str | None] = mapped_column(String(255))
    worksheet_auto_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    sheet_row: Mapped[int | None] = mapped_column(Integer)
    preview_values: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # target shift as seen at preview
    previous_values: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # full row before the write
    updated_values: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # values written
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    report: Mapped[OperationalReport] = relationship(back_populates="records")
    loader: Mapped[Loader | None] = relationship()

    __table_args__ = (
        UniqueConstraint("report_id", "loader_number", name="uq_record_report_loader"),
        Index("ix_records_status", "processing_status"),
    )

    @property
    def label(self) -> str:
        return f"Loader {self.loader_number:02d}"


class ShiftEntry(Base):
    """Logical report identity: the latest values applied for (loader, date, shift)."""

    __tablename__ = "shift_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    loader_id: Mapped[int] = mapped_column(ForeignKey("loaders.id", ondelete="CASCADE"))
    report_date: Mapped[date] = mapped_column(Date)
    shift: Mapped[int] = mapped_column(Integer)
    trips: Mapped[int | None] = mapped_column(Integer)
    quantity_mt: Mapped[Decimal | None] = mapped_column(ExactDecimal(14, 3))
    last_record_id: Mapped[int | None] = mapped_column(ForeignKey("loader_report_records.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("loader_id", "report_date", "shift", name="uq_shift_entry_identity"),)


# --- Infrastructure -------------------------------------------------------------------------


class Job(Base):
    """Durable background job (database-backed queue; survives restarts)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[JobStatus] = mapped_column(_enum(JobStatus), default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=6)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_jobs_status_run_after", "status", "run_after"),)


class ResourceLock(Base):
    """Lease-based mutex shared by every process using the same database."""

    __tablename__ = "resource_locks"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128))
    acquired_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)
