"""Loader and worksheet configuration (spreadsheet URLs, monthly tab mappings)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.database.models import Loader, LoaderWorksheet
from app.database.repositories import LoaderRepository, audit

_SPREADSHEET_URL_RE = re.compile(
    r"^https://docs\.google\.com/spreadsheets/(?:u/\d+/)?d/(?P<id>[A-Za-z0-9_-]{20,})(?:[/?#].*)?$"
)
_LOADER_TOKEN_RE = re.compile(r"^(?:loader|ldr|l)?[\s\-_#]*0*(?P<num>\d{1,3})$", re.I)


class ConfigurationError(ValueError):
    pass


def extract_spreadsheet_id(url: str) -> str:
    match = _SPREADSHEET_URL_RE.match(url.strip())
    if not match:
        raise ConfigurationError(
            "Not a Google Sheets URL. Expected https://docs.google.com/spreadsheets/d/<id>/edit"
        )
    return match.group("id")


def parse_loader_number(token: str) -> int:
    match = _LOADER_TOKEN_RE.match(token.strip())
    if not match or not 1 <= int(match.group("num")) <= 999:
        raise ConfigurationError(f"Invalid loader number '{token}'")
    return int(match.group("num"))


@dataclass(frozen=True)
class SetSheetResult:
    loader: Loader
    created: bool
    previous_url: str | None


class LoaderService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = LoaderRepository(session)

    def require(self, loader_number: int) -> Loader:
        loader = self.repo.get_by_number(loader_number)
        if loader is None:
            raise ConfigurationError(
                f"Loader {loader_number:02d} is not registered. Use /set_sheet {loader_number} <spreadsheet URL>"
            )
        return loader

    def set_sheet(self, loader_number: int, url: str, *, actor: int) -> SetSheetResult:
        spreadsheet_id = extract_spreadsheet_id(url)
        url = url.strip()
        loader = self.repo.get_by_number(loader_number)
        if loader is None:
            loader = Loader(loader_number=loader_number, spreadsheet_url=url, spreadsheet_id=spreadsheet_id, is_active=True)
            self.session.add(loader)
            self.session.flush()
            audit(self.session, "loader_created", actor=actor, entity_type="loader", entity_id=loader_number, url=url)
            return SetSheetResult(loader, True, None)
        previous = loader.spreadsheet_url
        loader.spreadsheet_url = url
        loader.spreadsheet_id = spreadsheet_id
        audit(
            self.session,
            "loader_sheet_updated",
            actor=actor,
            entity_type="loader",
            entity_id=loader_number,
            old_url=previous,
            new_url=url,
        )
        return SetSheetResult(loader, False, previous)

    def set_active(self, loader_number: int, active: bool, *, actor: int) -> Loader:
        loader = self.require(loader_number)
        loader.is_active = active
        audit(
            self.session,
            "loader_enabled" if active else "loader_disabled",
            actor=actor,
            entity_type="loader",
            entity_id=loader_number,
        )
        return loader

    def set_worksheet(self, loader_number: int, year: int, month: int, name: str, *, actor: int) -> tuple[LoaderWorksheet, str | None]:
        if not 2000 <= year <= 2100:
            raise ConfigurationError("Year must be between 2000 and 2100")
        if not 1 <= month <= 12:
            raise ConfigurationError("Month must be between 1 and 12")
        name = name.strip()
        if not name or len(name) > 100:
            raise ConfigurationError("Worksheet name must be 1-100 characters")
        loader = self.require(loader_number)
        mapping = self.repo.get_worksheet(loader.id, year, month)
        previous = None
        if mapping is None:
            mapping = LoaderWorksheet(loader_id=loader.id, year=year, month=month, worksheet_name=name, is_active=True)
            self.session.add(mapping)
        else:
            previous = mapping.worksheet_name
            mapping.worksheet_name = name
            mapping.is_active = True
        self.session.flush()
        audit(
            self.session,
            "worksheet_mapped",
            actor=actor,
            entity_type="loader",
            entity_id=loader_number,
            year=year,
            month=month,
            old_name=previous,
            new_name=name,
        )
        return mapping, previous

    def remove_worksheet(self, loader_number: int, year: int, month: int, *, actor: int) -> bool:
        loader = self.require(loader_number)
        mapping = self.repo.get_worksheet(loader.id, year, month)
        if mapping is None:
            return False
        self.session.delete(mapping)
        audit(
            self.session,
            "worksheet_unmapped",
            actor=actor,
            entity_type="loader",
            entity_id=loader_number,
            year=year,
            month=month,
            old_name=mapping.worksheet_name,
        )
        return True

    def configured_worksheet(self, loader: Loader, year: int, month: int) -> str | None:
        mapping = self.repo.get_worksheet(loader.id, year, month)
        return mapping.worksheet_name if mapping and mapping.is_active else None
