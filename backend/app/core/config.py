"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import json
import re
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CorrectionPolicy(str, Enum):
    """What to do when a report arrives for a loader/date/shift that already has data."""

    CONFIRM = "confirm"  # show old -> new values in the summary; the user's Confirm replaces them
    REJECT = "reject"  # never replace existing shift data through the bot
    ADMIN_APPROVAL = "admin_approval"  # replacements need an administrator's approval


# Logical spreadsheet columns and their default letters (see README "Spreadsheet layout").
DEFAULT_COLUMN_MAP: dict[str, str] = {
    "DATE": "B",
    "HSD_FUEL": "C",
    "SHIFT_1_TRIPS": "D",
    "SHIFT_1_WEIGHT": "E",
    "SHIFT_1_AVG": "F",
    "SHIFT_2_TRIPS": "G",
    "SHIFT_2_WEIGHT": "H",
    "SHIFT_2_AVG": "I",
    "SHIFT_3_TRIPS": "J",
    "SHIFT_3_WEIGHT": "K",
    "SHIFT_3_AVG": "L",
    "DAY_TOTAL_WEIGHT": "M",
    "DAY_TOTAL_TRIPS": "N",
}

_COLUMN_LETTERS_RE = re.compile(r"^[A-Z]{1,3}$")
_WEBHOOK_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"
    log_level: str = "INFO"
    log_format: str = "json"  # "json" or "text"

    # --- Telegram -------------------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_bot_username: str = ""  # optional; ignores "/cmd@OtherBot" in groups
    public_base_url: str = ""  # e.g. https://bot.example.com (used by scripts/set_webhook.py)
    webhook_path: str = "/telegram/webhook"
    admin_telegram_ids: str = ""  # comma-separated Telegram user ids that are always administrators
    allow_group_submissions: bool = False
    confirmation_ttl_minutes: int = Field(default=120, ge=1)
    max_webhook_body_bytes: int = 1_000_000

    # --- Database -------------------------------------------------------------------------
    database_url: str = "sqlite:///./data/app.db"
    db_url: str = ""  # alias for DATABASE_URL; set one of the two

    # --- Google Sheets --------------------------------------------------------------------
    google_service_account_file: str = ""
    google_service_account_json: str = ""  # alternative to the file: the raw JSON key
    google_api_timeout_seconds: float = 30.0
    google_api_max_attempts: int = Field(default=5, ge=1)

    # Worksheet / layout handling
    worksheet_auto_detect: bool = True  # pick a tab by loader+month+year only when exactly one matches
    sheet_header_detection: bool = True
    sheet_header_strict: bool = True  # every required column must be found by its header text
    sheet_header_scan_rows: int = Field(default=20, ge=1)
    sheet_read_max_rows: int = Field(default=150, ge=10)
    sheet_read_last_column: str = "Z"
    sheet_column_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_COLUMN_MAP))

    # Calculations / validation
    weight_decimals: int = Field(default=3, ge=0, le=6)
    average_decimals: int = Field(default=2, ge=0, le=6)
    totals_tolerance_mt: str = "0.001"
    timezone: str = "Asia/Kolkata"
    correction_policy: CorrectionPolicy = CorrectionPolicy.CONFIRM

    # --- Background worker ----------------------------------------------------------------
    run_worker_in_process: bool = True
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    job_max_attempts: int = Field(default=6, ge=1)
    job_lease_seconds: int = Field(default=600, ge=30)
    job_retry_base_seconds: float = Field(default=15.0, gt=0)
    sheet_lock_ttl_seconds: int = Field(default=300, ge=10)
    sheet_lock_wait_seconds: float = Field(default=120.0, gt=0)

    @field_validator("database_url", "db_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        # Render/Heroku/Neon-style URLs (postgres://, postgresql://) would select psycopg2, which
        # is not installed; use the psycopg 3 driver from requirements.txt instead.
        value = value.strip()
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value

    @field_validator("sheet_column_map", mode="before")
    @classmethod
    def _parse_column_map(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else {}
        return value

    @field_validator("sheet_column_map")
    @classmethod
    def _merge_column_map(cls, value: dict[str, str]) -> dict[str, str]:
        merged = dict(DEFAULT_COLUMN_MAP)
        for key, letter in value.items():
            key_u = key.strip().upper()
            if key_u not in DEFAULT_COLUMN_MAP:
                raise ValueError(f"Unknown column key in SHEET_COLUMN_MAP: {key}")
            letter_u = letter.strip().upper()
            if not _COLUMN_LETTERS_RE.match(letter_u):
                raise ValueError(f"Invalid column letter for {key}: {letter}")
            merged[key_u] = letter_u
        if len(set(merged.values())) != len(merged):
            raise ValueError("SHEET_COLUMN_MAP assigns the same column letter to two fields")
        return merged

    @field_validator("sheet_read_last_column")
    @classmethod
    def _validate_last_column(cls, value: str) -> str:
        value = value.strip().upper()
        if not _COLUMN_LETTERS_RE.match(value):
            raise ValueError("SHEET_READ_LAST_COLUMN must be a column letter such as Z")
        return value

    @model_validator(mode="after")
    def _validate_secret(self) -> "Settings":
        if self.telegram_webhook_secret and not _WEBHOOK_SECRET_RE.match(self.telegram_webhook_secret):
            raise ValueError(
                "TELEGRAM_WEBHOOK_SECRET may contain only A-Z, a-z, 0-9, '_' and '-' (1-256 chars)"
            )
        return self

    @model_validator(mode="after")
    def _resolve_db_url(self) -> "Settings":
        if self.db_url:
            if "database_url" in self.model_fields_set and self.database_url != self.db_url:
                raise ValueError("DATABASE_URL and DB_URL point to different databases; set only one of them")
            self.database_url = self.db_url
        return self

    # --- Derived helpers ------------------------------------------------------------------
    @property
    def admin_ids(self) -> frozenset[int]:
        ids: set[int] = set()
        for part in re.split(r"[,\s]+", self.admin_telegram_ids.strip()):
            if part:
                ids.add(int(part))
        return frozenset(ids)

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() in {"dev", "development", "local", "test"}

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix) and ":memory:" not in self.database_url:
            return Path(self.database_url[len(prefix):])
        return None

    def missing_runtime_settings(self) -> list[str]:
        """Settings the server cannot run without (checked at startup, not at import)."""
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_webhook_secret:
            missing.append("TELEGRAM_WEBHOOK_SECRET")
        if not (self.google_service_account_file or self.google_service_account_json):
            missing.append("GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON")
        if not self.admin_ids:
            missing.append("ADMIN_TELEGRAM_IDS")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
