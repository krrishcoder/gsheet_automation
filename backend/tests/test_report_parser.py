from datetime import date
from decimal import Decimal

import pytest

from app.services.report_parser import ReportParseError, normalize_text, parse_decimal, parse_report
from tests.conftest import SAMPLE_REPORT


def _report(shift_line: str = "*SHIFT*:- 1st Shift", date_line: str = "*DATE*:- 06/11/2026", body: str | None = None) -> str:
    body = body if body is not None else "*LOADER - 11*\nTrip:- 21 Qty:-696.380"
    return f"*LOADER REPORT - CHP*\n{date_line}\n{shift_line}\n{body}"


def test_sample_report_extracts_every_loader():
    report = parse_report(SAMPLE_REPORT)
    assert report.report_date == date(2026, 11, 6)
    assert report.shift == 1
    assert [(e.loader_number, e.trips, e.quantity_mt) for e in report.loaders] == [
        (1, 42, Decimal("1340.840")),
        (5, 43, Decimal("1411.930")),
        (11, 21, Decimal("696.380")),
    ]
    assert report.reported_total_trips == 106
    assert report.reported_total_quantity_mt == Decimal("3449.150")


def test_pc_equipment_is_never_a_loader():
    report = parse_report(SAMPLE_REPORT)
    assert {e.loader_number for e in report.loaders} == {1, 5, 11}
    assert [(o.category, o.number) for o in report.other_equipment] == [("PC", 15), ("PC", 21)]


def test_nil_is_unavailable_not_zero():
    report = parse_report(SAMPLE_REPORT)
    pc = report.other_equipment[0]
    assert pc.trips is None and pc.quantity_mt is None
    assert pc.trips_nil and pc.quantity_nil
    assert not pc.has_data


def test_genuine_zero_is_kept():
    report = parse_report(_report(body="LOADER - 08\nTrip:- 0 Qty:- 0"))
    assert report.loaders[0].trips == 0
    assert report.loaders[0].quantity_mt == Decimal("0")
    assert report.loaders[0].has_data


@pytest.mark.parametrize(
    "shift_line, expected",
    [
        ("*SHIFT*:- 1st Shift", 1),
        ("*SHIFT*:- 1st shift", 1),
        ("*SHIFT* :-   1st    Shift  ", 1),
        ("SHIFT:-1ST SHIFT", 1),
        ("*SHIFT*:- 2nd Shift", 2),
        ("*SHIFT*:- 2nd.shipt", 2),
        ("Shift - II", 2),
        ("*SHIFT*:- 3rd Shift", 3),
        ("*SHIFT*:- 3rd shift", 3),
        ("Shift: third", 3),
        ("3rd Shift", 3),  # no label at all
    ],
)
def test_shift_variants(shift_line, expected):
    assert parse_report(_report(shift_line=shift_line)).shift == expected


@pytest.mark.parametrize("shift_line", ["*SHIFT*:- 4th Shift", "*SHIFT*:- General", "*SHIFT*:- B"])
def test_unrecognised_shift_is_rejected(shift_line):
    with pytest.raises(ReportParseError) as exc:
        parse_report(_report(shift_line=shift_line))
    assert any("SHIFT" in e for e in exc.value.errors)


def test_missing_shift_is_rejected():
    with pytest.raises(ReportParseError) as exc:
        parse_report(_report(shift_line=""))
    assert any("SHIFT not found" in e for e in exc.value.errors)


@pytest.mark.parametrize(
    "date_line",
    [
        "*DATE*:- 06/11/2026",
        "DATE:-06-11-2026",
        "Date : 6.11.2026",
        "date - 06/11/26",
        "DATE:- 2026-11-06",
        "DATE:- 06 Nov 2026",
        "Date: 6th November, 2026",
        "Date: Nov 6, 2026",
    ],
)
def test_date_formats_normalise_to_iso(date_line):
    report = parse_report(_report(date_line=date_line))
    assert report.report_date.isoformat() == "2026-11-06"


@pytest.mark.parametrize("date_line, fragment", [("DATE:- 31/02/2026", "Invalid DATE"), ("", "DATE not found")])
def test_bad_or_missing_date(date_line, fragment):
    with pytest.raises(ReportParseError) as exc:
        parse_report(_report(date_line=date_line))
    assert any(fragment in e for e in exc.value.errors)


def test_formatting_variations_and_single_line_layout():
    text = "loader report\ndate:-06/11/2026 shift:-1st shift\nLoader-01 trip:-42 qty:-1340.84 LOADER 05 TRIPS : 43 QTY = 1,411.930 MT"
    report = parse_report(text)
    assert [(e.loader_number, e.trips, e.quantity_mt) for e in report.loaders] == [
        (1, 42, Decimal("1340.84")),
        (5, 43, Decimal("1411.930")),
    ]


def test_unicode_dashes_and_whatsapp_markup():
    text = "*DATE*:– 06/11/2026\n_SHIFT_ :— 1st Shift\n*LOADER – 11*\\\nTrip:– 21 Qty:–696.380"
    report = parse_report(text)
    assert report.loaders[0].quantity_mt == Decimal("696.380")


def test_partial_nil_values():
    report = parse_report(_report(body="LOADER - 11\nTrip:- 21 Qty:- Nil"))
    entry = report.loaders[0]
    assert entry.trips == 21 and entry.quantity_mt is None and entry.quantity_nil


def test_missing_loader_entries_are_simply_absent():
    report = parse_report(_report(body="LOADER - 05\nTrip:- 3 Qty:- 90"))
    assert [e.loader_number for e in report.loaders] == [5]


def test_loader_without_values_warns():
    report = parse_report(_report(body="LOADER - 05\nLOADER - 11\nTrip:- 21 Qty:-696.380"))
    assert not report.loaders[0].has_data
    assert any("Loader 05" in w for w in report.warnings)


def test_duplicate_loader_is_flagged():
    report = parse_report(_report(body="LOADER - 11\nTrip:- 21 Qty:- 5\nLOADER - 11\nTrip:- 22 Qty:- 6"))
    assert len(report.loaders) == 1
    assert "listed 2 times" in report.loaders[0].error


def test_ambiguous_number_is_an_error_not_a_guess():
    report = parse_report(_report(body="LOADER - 11\nTrip:- 21 Qty:- 696,380"))
    entry = report.loaders[0]
    assert entry.quantity_mt is None
    assert "ambiguous" in entry.error


def test_fractional_trips_rejected():
    report = parse_report(_report(body="LOADER - 11\nTrip:- 21.5 Qty:- 696"))
    assert "whole number" in report.loaders[0].error


def test_not_a_report():
    with pytest.raises(ReportParseError) as exc:
        parse_report("hello there")
    assert len(exc.value.errors) == 3  # date, shift, loaders


def test_parse_decimal_grouping():
    assert parse_decimal("1,34,084.500") == Decimal("134084.500")
    with pytest.raises(ValueError):
        parse_decimal("1.2.3")


def test_normalize_text_strips_markup():
    assert normalize_text("*DATE*:- 06/11/2026\r\n\n  _x_  ") == "DATE:- 06/11/2026\nx"
