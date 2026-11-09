"""Data-access helpers. Each repository wraps a Session; callers own the transaction."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database.models import (
    AuditLog,
    Job,
    JobStatus,
    Loader,
    LoaderReportRecord,
    LoaderWorksheet,
    OperationalReport,
    RecordStatus,
    ReportStatus,
    ShiftEntry,
    TelegramUpdate,
    TrustedChat,
    User,
    UserRole,
    user_loaders,
    utcnow,
)


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_telegram_id(self, telegram_user_id: int) -> User | None:
        return self.session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id).options(selectinload(User.loaders))
        )

    def get_or_create(self, telegram_user_id: int, *, role: UserRole = UserRole.USER) -> tuple[User, bool]:
        user = self.get_by_telegram_id(telegram_user_id)
        if user:
            return user, False
        user = User(telegram_user_id=telegram_user_id, role=role, is_active=True)
        self.session.add(user)
        self.session.flush()
        return user, True

    def list_all(self) -> list[User]:
        return list(
            self.session.scalars(select(User).options(selectinload(User.loaders)).order_by(User.role, User.id))
        )

    def list_admins(self) -> list[User]:
        return list(
            self.session.scalars(select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True)))
        )

    def grant_loader(self, user: User, loader: Loader, granted_by: int | None) -> bool:
        exists = self.session.execute(
            select(user_loaders.c.user_id).where(
                user_loaders.c.user_id == user.id, user_loaders.c.loader_id == loader.id
            )
        ).first()
        if exists:
            return False
        self.session.execute(
            insert(user_loaders).values(
                user_id=user.id, loader_id=loader.id, granted_by=granted_by, granted_at=utcnow()
            )
        )
        self.session.expire(user, ["loaders"])
        return True

    def revoke_loader(self, user: User, loader: Loader) -> bool:
        result = self.session.execute(
            delete(user_loaders).where(user_loaders.c.user_id == user.id, user_loaders.c.loader_id == loader.id)
        )
        self.session.expire(user, ["loaders"])
        return result.rowcount > 0

    def has_loader(self, user: User, loader_id: int) -> bool:
        return (
            self.session.execute(
                select(user_loaders.c.user_id).where(
                    user_loaders.c.user_id == user.id, user_loaders.c.loader_id == loader_id
                )
            ).first()
            is not None
        )


class LoaderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_number(self, loader_number: int) -> Loader | None:
        return self.session.scalar(select(Loader).where(Loader.loader_number == loader_number))

    def list_all(self) -> list[Loader]:
        return list(
            self.session.scalars(
                select(Loader).options(selectinload(Loader.worksheets)).order_by(Loader.loader_number)
            )
        )

    def get_worksheet(self, loader_id: int, year: int, month: int) -> LoaderWorksheet | None:
        return self.session.scalar(
            select(LoaderWorksheet).where(
                LoaderWorksheet.loader_id == loader_id,
                LoaderWorksheet.year == year,
                LoaderWorksheet.month == month,
            )
        )

    def list_worksheets(self, loader_id: int) -> list[LoaderWorksheet]:
        return list(
            self.session.scalars(
                select(LoaderWorksheet)
                .where(LoaderWorksheet.loader_id == loader_id)
                .order_by(LoaderWorksheet.year.desc(), LoaderWorksheet.month.desc())
            )
        )


class TrustedChatRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, chat_id: int) -> TrustedChat | None:
        return self.session.scalar(select(TrustedChat).where(TrustedChat.chat_id == chat_id))

    def is_trusted(self, chat_id: int) -> bool:
        chat = self.get(chat_id)
        return bool(chat and chat.is_active)

    def list_all(self) -> list[TrustedChat]:
        return list(self.session.scalars(select(TrustedChat).order_by(TrustedChat.id)))


class UpdateRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, update_id: int) -> TelegramUpdate | None:
        return self.session.get(TelegramUpdate, update_id)


class ReportRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, report_id: int, *, with_records: bool = True) -> OperationalReport | None:
        stmt = select(OperationalReport).where(OperationalReport.id == report_id)
        if with_records:
            stmt = stmt.options(selectinload(OperationalReport.records))
        return self.session.scalar(stmt)

    def get_by_update(self, update_id: int) -> OperationalReport | None:
        return self.session.scalar(
            select(OperationalReport)
            .where(OperationalReport.source_update_id == update_id)
            .options(selectinload(OperationalReport.records))
        )

    def get_record(self, record_id: int) -> LoaderReportRecord | None:
        return self.session.get(LoaderReportRecord, record_id)

    def recent(self, *, limit: int = 10, telegram_user_id: int | None = None) -> list[OperationalReport]:
        stmt = select(OperationalReport).options(selectinload(OperationalReport.records))
        if telegram_user_id is not None:
            stmt = stmt.where(OperationalReport.telegram_user_id == telegram_user_id)
        return list(self.session.scalars(stmt.order_by(OperationalReport.id.desc()).limit(limit)))

    def transition_report(
        self, report_id: int, from_statuses: Iterable[ReportStatus], to_status: ReportStatus, **values: Any
    ) -> bool:
        """Compare-and-set status change; returns False if another actor got there first."""
        result = self.session.execute(
            update(OperationalReport)
            .where(OperationalReport.id == report_id, OperationalReport.status.in_(list(from_statuses)))
            .values(status=to_status, updated_at=utcnow(), **values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def transition_record(
        self, record_id: int, from_statuses: Iterable[RecordStatus], to_status: RecordStatus, **values: Any
    ) -> bool:
        result = self.session.execute(
            update(LoaderReportRecord)
            .where(LoaderReportRecord.id == record_id, LoaderReportRecord.processing_status.in_(list(from_statuses)))
            .values(processing_status=to_status, updated_at=utcnow(), **values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def stale_pending_confirmation(self, older_than: datetime) -> list[OperationalReport]:
        return list(
            self.session.scalars(
                select(OperationalReport).where(
                    OperationalReport.status == ReportStatus.PENDING_CONFIRMATION,
                    OperationalReport.created_at < older_than,
                )
            )
        )


class ShiftEntryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, loader_id: int, report_date: date, shift: int) -> ShiftEntry | None:
        return self.session.scalar(
            select(ShiftEntry).where(
                ShiftEntry.loader_id == loader_id,
                ShiftEntry.report_date == report_date,
                ShiftEntry.shift == shift,
            )
        )

    def upsert(
        self,
        *,
        loader_id: int,
        report_date: date,
        shift: int,
        trips: int | None,
        quantity_mt: Decimal | None,
        record_id: int,
    ) -> ShiftEntry:
        entry = self.get(loader_id, report_date, shift)
        if entry is None:
            entry = ShiftEntry(
                loader_id=loader_id,
                report_date=report_date,
                shift=shift,
                trips=trips,
                quantity_mt=quantity_mt,
                last_record_id=record_id,
                revision=1,
            )
            self.session.add(entry)
            try:
                with self.session.begin_nested():
                    self.session.flush()
                return entry
            except IntegrityError:
                entry = self.get(loader_id, report_date, shift)
                assert entry is not None
        entry.trips = trips
        entry.quantity_mt = quantity_mt
        entry.last_record_id = record_id
        entry.revision += 1
        return entry


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def counts(self) -> dict[str, int]:
        rows = self.session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        return {status.value if hasattr(status, "value") else str(status): count for status, count in rows}

    def has_dead(self) -> bool:
        return bool(self.session.scalar(select(func.count()).where(Job.status == JobStatus.DEAD)))


def audit(
    session: Session,
    action: str,
    *,
    actor: int | None,
    entity_type: str | None = None,
    entity_id: Any = None,
    **details: Any,
) -> None:
    session.add(
        AuditLog(
            actor_telegram_user_id=actor,
            action=action,
            entity_type=entity_type,
            entity_id=None if entity_id is None else str(entity_id),
            details=_jsonable(details),
        )
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (int, float, str, bool)):
        return value.value
    return value
