"""Engine/session management.

SQLite is the default store. For SQLite we:
* enable WAL so the webhook can insert while the worker reads,
* set a busy timeout,
* start every transaction with ``BEGIN IMMEDIATE`` so concurrent writers wait for the lock
  instead of failing with "database is locked" when upgrading a read transaction.

Transactions are kept short on purpose: no session is held open across Google or Telegram
API calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.models import Base


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    if database_url.startswith("sqlite"):
        in_memory = database_url in {"sqlite://", "sqlite:///:memory:"}
        if not in_memory:
            db_path = Path(database_url.split("sqlite:///", 1)[1])
            db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            database_url,
            echo=echo,
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=StaticPool if in_memory else None,
        )
        _configure_sqlite(engine, wal=not in_memory)
        return engine
    connect_args = {}
    if database_url.startswith("postgresql+psycopg"):
        # Transaction-mode poolers (PgBouncer, Neon "-pooler", Supabase :6543) hand each
        # transaction to any server connection, so psycopg's auto-prepared statements can vanish.
        connect_args["prepare_threshold"] = None
    # pre_ping + recycle: serverless databases (e.g. Neon) drop idle connections when they suspend.
    return create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
        connect_args=connect_args,
    )


def _configure_sqlite(engine: Engine, *, wal: bool) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _record):  # pragma: no cover - exercised implicitly
        # Let SQLAlchemy (not pysqlite) control BEGIN, see the module docstring.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        if wal:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):  # pragma: no cover - exercised implicitly
        conn.exec_driver_sql("BEGIN IMMEDIATE")


class Database:
    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.url = database_url
        self.engine = build_engine(database_url, echo=echo)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """create_all() never changes a table that already exists, so a nullable column added to a
        model later is added here (the only kind of schema change this app makes)."""
        inspector = inspect(self.engine)
        dialect = self.engine.dialect
        statements = []
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                if not column.nullable:
                    raise RuntimeError(f"Cannot add NOT NULL column {table.name}.{column.name} automatically")
                guard = "IF NOT EXISTS " if dialect.name == "postgresql" else ""
                statements.append(
                    f"ALTER TABLE {table.name} ADD COLUMN {guard}{column.name} {column.type.compile(dialect)}"
                )
        if statements:
            with self.engine.begin() as conn:
                for statement in statements:
                    conn.execute(text(statement))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Unit of work: commits on success, rolls back on any exception."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> bool:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    def dispose(self) -> None:
        self.engine.dispose()
