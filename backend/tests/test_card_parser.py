from datetime import date
from decimal import Decimal

import pytest

from app.services.card_parser import CARD_FORMAT, CardParseError, Refill, is_card_message, parse_card

TODAY = date(2026, 11, 10)
D = date(2026, 11, 6)


def dec(values: dict) -> dict:
    return {k: Decimal(v) for k, v in values.items()}


# --- Short codes ----------------------------------------------------------------------------------


def test_full_day_in_short_codes():
    card = parse_card(CARD_FORMAT, today=TODAY)
    assert (card.loader_number, card.entry_date) == (11, TODAY)
    assert card.trips == {1: 37, 2: 35, 3: 40}
    assert card.opening == dec({1: "4268", 2: "4272", 3: "4276.2"})
    assert card.closing == dec({1: "4272", 2: "4276.2", 3: "4280"})
    assert card.refills == [Refill(Decimal("132.4"), Decimal("4276.2"))]


@pytest.mark.parametrize(
    "text, trips, opening, closing, refills",
    [
        ("L11 10-11\nT1 37 O1 4268 C1 4276.2", {1: 37}, {1: "4268"}, {1: "4276.2"}, []),  # after one shift
        ("L11 10-11\nH 132.4 4276.2", {}, {}, {}, [("132.4", "4276.2")]),  # refill only
        ("L11 10-11\nT2 35 O2 4276.2 C2 4284\nH 120 4284", {2: 35}, {2: "4276.2"}, {2: "4284"}, [("120", "4284")]),
        ("L11 10-11 C3 4291.5", {}, {}, {3: "4291.5"}, []),  # one line, one value
        ("L11 10-11 T3 40 H 120 4291.5 H 50 4295", {3: 40}, {}, {}, [("120", "4291.5"), ("50", "4295")]),
        ("*L11 10-11*\nt1:37\no1-4268\nc1 4276.2\nh 132.4 L @ 4276.2", {1: 37}, {1: "4268"}, {1: "4276.2"}, [("132.4", "4276.2")]),
        ("L11 10/11/2026 O1 4268", {}, {1: "4268"}, {}, []),
        ("Loader 11 10-11\nT1 nil T2 0", {2: 0}, {}, {}, []),  # nil = nothing to save, 0 = zero trips
    ],
)
def test_any_mix_of_codes(text, trips, opening, closing, refills):
    assert is_card_message(text)
    card = parse_card(text, today=TODAY)
    assert (card.loader_number, card.entry_date) == (11, TODAY)
    assert card.trips == trips and card.opening == dec(opening) and card.closing == dec(closing)
    assert card.refills == [Refill(Decimal(l), Decimal(hm)) for l, hm in refills]


# --- Long form (still accepted, can be mixed with codes) ------------------------------------------


@pytest.mark.parametrize(
    "text, trips, hsd",
    [
        ("CARD L11\nDate: 06-11-2026\n1st: 36\n2nd: 37\n3rd: 44\nHSD: 190 + 154.98", {1: 36, 2: 37, 3: 44}, ["190", "154.98"]),
        ("CARD L11\nDate: 06-11-2026\n3rd: 44", {3: 44}, []),
        ("CARD L11 06-11 3rd 44 HSD 154.98", {3: 44}, ["154.98"]),
        ("*CARD L11*\n*Date*:- 06/11/2026\n1st shift trips - 36\n2nd Shift:- 37", {1: 36, 2: 37}, []),
        ("Card Loader-11 06-11-26\nHSD (L): 190 L + 154.98 L", {}, ["190", "154.98"]),
        ("CARD L 11\nDate: 6/11\n1st 36 2nd 37", {1: 36, 2: 37}, []),
        ("CARD L11\nDate: 06-11-2026\n1st: nil\n3rd: 44\nDiesel 99.06, 121.11", {3: 44}, ["99.06", "121.11"]),
        ("CARD\nLoader: 11\nDate: 06-11-2026\n3rd 44HSD 154.98", {3: 44}, ["154.98"]),
        ("L11 06-11\nT1 36\n2nd: 37", {1: 36, 2: 37}, []),
    ],
)
def test_long_form(text, trips, hsd):
    assert is_card_message(text)
    card = parse_card(text, today=TODAY)
    assert (card.loader_number, card.entry_date) == (11, D)
    assert card.trips == trips
    assert card.hsd_fills == [Decimal(v) for v in hsd]
    assert all(r.hm is None for r in card.refills)  # an HSD: line has no hour meter


def test_date_without_year_is_the_latest_past_one():
    assert parse_card("L11 28-12 T1 3", today=date(2027, 1, 2)).entry_date == date(2026, 12, 28)


@pytest.mark.parametrize(
    "text, error",
    [
        ("L11 10-11\nT137", "Could not read 'T137'"),  # no space: not read as T1 = 37
        ("L11 10-11\n01 4268", "Could not read '01'"),  # zero-one instead of the letter O
        ("L11 10-11 X 55", "Could not read '55'"),
        ("L11 10-11\nH 132.4", "HM missing"),
        ("L11 10-11\nH", "H: litres missing"),
        ("L11 10-11\nHSD 132.4 4276.2", "put + between fills, or use 'H <litres> <HM>'"),
        ("L11 10-11\nO1 4268 O1 4270", "O1 (opening HM) given twice"),
        ("L11 10-11\nT1", "T1: no number after it"),
        ("L11 10-11\nT1 36.5", "trips must be a whole number"),
        ("L11\nT1 36", "Date missing"),
        ("CARD\nT1 36", "Loader missing"),
        ("L11 10-11\n", "Nothing to save"),
        ("L11 12-11-2026 T1 3", "in the future"),
        ("L11 31-02-2026 T1 3", "is not a valid date"),
        ("CARD L11 06-11\nHSD: abc", "HSD: unreadable number 'abc'"),
        ("CARD L11 06-11\n1st 36\n1st 38", "1st shift trips given twice"),
    ],
)
def test_errors_are_reported_not_guessed(text, error):
    with pytest.raises(CardParseError) as exc:
        parse_card(text, today=TODAY)
    assert any(error in e for e in exc.value.errors), exc.value.errors


def test_which_messages_are_card_messages():
    assert is_card_message("L11 10-11") and is_card_message("L11") and is_card_message("LDR 11 10-11")
    assert is_card_message("Loader 11 10-11") and is_card_message("Trip card L11 06-11 1st 36")
    # CHP reports and their loader sections are not
    assert not is_card_message("*LOADER REPORT - CHP*\n*DATE*:- 06/11/2026\n*SHIFT*:- 1st Shift")
    assert not is_card_message("*LOADER - 01*\nTrip:- 42 Qty:-1340.840")
    assert not is_card_message("Loader 11\nTrip card 36")
