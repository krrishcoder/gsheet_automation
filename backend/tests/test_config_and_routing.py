"""Worksheet selection, loader/URL configuration and report validation rules."""

from datetime import date
from decimal import Decimal

import pytest

from app.core.config import Settings
from app.schemas.report import IssueSeverity
from app.services.loader_service import ConfigurationError, extract_spreadsheet_id, parse_loader_number
from app.services.report_parser import parse_report
from app.services.validation_service import validate_report
from app.services.worksheet_service import WorksheetResolutionError, parse_month, resolve_worksheet, title_matches
from tests.conftest import SAMPLE_REPORT

TABS = ["Loader 11 nov 2026", "loader 08 Nov 2026", "Loader 11 oct 2026", "Oct loader 08 2026", "Loader 12", "Sept 2026"]


def resolve(loader, year, month, configured=None, titles=TABS, auto=True):
    return resolve_worksheet(
        loader_number=loader, year=year, month=month, configured_name=configured, available_titles=titles, auto_detect=auto
    )


# --- Worksheets -------------------------------------------------------------------------------


def test_configured_mapping_wins():
    res = resolve(11, 2026, 11, configured="Loader 11 oct 2026")
    assert res.worksheet_name == "Loader 11 oct 2026" and not res.auto_detected


def test_configured_tab_missing_is_an_error_never_a_fallback():
    with pytest.raises(WorksheetResolutionError) as exc:
        resolve(11, 2026, 11, configured="loader 11 NOV 2026 ")
    assert exc.value.code == "WORKSHEET_NOT_FOUND"
    assert "Loader 11 nov 2026" in exc.value.message  # hint at the near match


@pytest.mark.parametrize(
    "loader, month, expected",
    [(11, 11, "Loader 11 nov 2026"), (8, 11, "loader 08 Nov 2026"), (11, 10, "Loader 11 oct 2026"), (8, 10, "Oct loader 08 2026")],
)
def test_auto_detection_on_screenshot_tabs(loader, month, expected):
    res = resolve(loader, 2026, month)
    assert res.worksheet_name == expected and res.auto_detected


@pytest.mark.parametrize("loader, month", [(12, 11), (5, 11), (11, 12), (12, 9)])
def test_auto_detection_refuses_to_guess(loader, month):
    with pytest.raises(WorksheetResolutionError) as exc:
        resolve(loader, 2026, month)
    assert exc.value.code == "WORKSHEET_NOT_CONFIGURED"


def test_auto_detection_ambiguous():
    with pytest.raises(WorksheetResolutionError) as exc:
        resolve(11, 2026, 11, titles=["Loader 11 nov 2026", "Copy of Loader 11 November 2026"])
    assert exc.value.code == "WORKSHEET_AMBIGUOUS"


def test_auto_detection_can_be_disabled():
    with pytest.raises(WorksheetResolutionError):
        resolve(11, 2026, 11, auto=False)


def test_title_matching_rules():
    assert title_matches("Loader11-Nov-2026", loader_number=11, year=2026, month=11)
    assert not title_matches("Loader 11 & 12 nov 2026", loader_number=11, year=2026, month=11)
    assert not title_matches("Loader 11 nov 2025", loader_number=11, year=2026, month=11)
    assert parse_month("Sept") == 9 and parse_month("11") == 11 and parse_month("13") is None


# --- Loader configuration ---------------------------------------------------------------------


def test_spreadsheet_url_validation():
    sid = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
    assert extract_spreadsheet_id(f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid=0") == sid
    assert extract_spreadsheet_id(f"https://docs.google.com/spreadsheets/u/1/d/{sid}") == sid
    for bad in ("https://example.com/spreadsheets/d/" + sid, "docs.google.com/spreadsheets/d/" + sid, "https://docs.google.com/document/d/" + sid):
        with pytest.raises(ConfigurationError):
            extract_spreadsheet_id(bad)


@pytest.mark.parametrize("token, number", [("11", 11), ("01", 1), ("Loader-08", 8), ("L12", 12)])
def test_loader_number_tokens(token, number):
    assert parse_loader_number(token) == number


@pytest.mark.parametrize("token", ["0", "abc", "1000"])
def test_invalid_loader_numbers(token):
    with pytest.raises(ConfigurationError):
        parse_loader_number(token)


def test_settings_column_map_override_and_validation():
    s = Settings(_env_file=None, sheet_column_map='{"date": "a"}')
    assert s.sheet_column_map["DATE"] == "A" and s.sheet_column_map["SHIFT_1_TRIPS"] == "D"
    with pytest.raises(ValueError):
        Settings(_env_file=None, sheet_column_map='{"DATE": "D"}')  # collides with SHIFT_1_TRIPS
    with pytest.raises(ValueError):
        Settings(_env_file=None, telegram_webhook_secret="has spaces")
    assert Settings(_env_file=None, admin_telegram_ids="1, 2 3").admin_ids == {1, 2, 3}


# --- Report validation ----------------------------------------------------------------------------


def _validate(text, today=date(2026, 11, 6)):
    return validate_report(parse_report(text), today=today, tolerance_mt=Decimal("0.001"))


def test_totals_match_sample():
    assert [i.code for i in _validate(SAMPLE_REPORT)] == []


def test_totals_mismatch_is_flagged_not_corrected():
    text = SAMPLE_REPORT.replace("Total Trip*:-106", "Total Trip*:-107").replace("3449.150", "3449.000")
    report = parse_report(text)
    issues = validate_report(report, today=date(2026, 11, 6), tolerance_mt=Decimal("0.001"))
    assert {i.code for i in issues} == {"TOTAL_TRIPS_MISMATCH", "TOTAL_QTY_MISMATCH"}
    assert all(i.severity == IssueSeverity.WARNING for i in issues)
    assert [e.trips for e in report.loaders] == [42, 43, 21]  # untouched


def test_loader_level_errors_and_future_date():
    text = "DATE:- 20/11/2026\nSHIFT:- 1st\nLOADER - 01\nTrip:- 0 Qty:- 10\nLOADER - 05\nTrip:- 3 Qty:- Nil"
    issues = {(i.code, i.loader_number, i.severity) for i in _validate(text)}
    assert ("QTY_WITHOUT_TRIPS", 1, IssueSeverity.ERROR) in issues
    assert ("PARTIAL_VALUES", 5, IssueSeverity.WARNING) in issues
    assert ("FUTURE_DATE", None, IssueSeverity.WARNING) in issues


def test_db_url_alias_and_postgres_normalisation():
    neon = "postgresql://u:p@ep-x-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
    s = Settings(_env_file=None, db_url=neon)
    assert s.database_url == "postgresql+psycopg://u:p@ep-x-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
    assert Settings(_env_file=None, database_url="postgres://u@h/d").database_url == "postgresql+psycopg://u@h/d"
    assert Settings(_env_file=None).database_url.startswith("sqlite")
    with pytest.raises(ValueError, match="set only one"):
        Settings(_env_file=None, database_url="sqlite:///x.db", db_url=neon)


def test_postgres_engine_disables_prepared_statements_for_poolers(monkeypatch):
    from app.database import database as db_module

    captured = {}
    original = db_module.create_engine
    monkeypatch.setattr(db_module, "create_engine", lambda url, **kw: captured.update(kw) or original(url, **kw))
    db_module.build_engine("postgresql+psycopg://u:p@localhost:6543/db").dispose()  # no connection is made
    assert captured["connect_args"] == {"prepare_threshold": None}
    assert captured["pool_pre_ping"] is True
