from datetime import date
from decimal import Decimal

import pytest

from app.services.card_parser import CARD_FORMAT, CardParseError, is_card_message, parse_card

TODAY = date(2026, 11, 10)
D = date(2026, 11, 6)


@pytest.mark.parametrize(
    "text, trips, hsd",
    [
        (CARD_FORMAT, {1: 36, 2: 37, 3: 44}, ["190", "154.98"]),
        ("CARD L11\nDate: 06-11-2026\n3rd: 44", {3: 44}, []),  # one shift
        ("CARD L11\nDate: 06-11-2026\n1st: 36\n2nd: 37", {1: 36, 2: 37}, []),  # two shifts
        ("CARD L11 06-11-2026\nHSD 190", {}, ["190"]),  # HSD only
        ("CARD L11 06-11 3rd 44 HSD 154.98", {3: 44}, ["154.98"]),  # one line, no year
        ("*CARD L11*\n*Date*:- 06/11/2026\n1st shift trips - 36\n2nd Shift:- 37", {1: 36, 2: 37}, []),  # WhatsApp bold
        ("Card Loader-11 06-11-26\nHSD (L): 190 L + 154.98 L", {}, ["190", "154.98"]),
        ("CARD L 11\nDate: 6/11\n1st 36 2nd 37", {1: 36, 2: 37}, []),
        ("CARD L11\nDate: 06-11-2026\n1st: nil\n3rd: 44\nDiesel 99.06, 121.11", {3: 44}, ["99.06", "121.11"]),
        ("CARD L11\nDate 06-11-2026\nShift 2: 37\nFirst: 0", {1: 0, 2: 37}, []),
        ("CARD\nLoader: 11\nDate: 06-11-2026\n3rd 44HSD 154.98", {3: 44}, ["154.98"]),
    ],
)
def test_any_combination_of_shifts_and_hsd(text, trips, hsd):
    assert is_card_message(text)
    card = parse_card(text, today=TODAY)
    assert (card.loader_number, card.entry_date) == (11, D)
    assert card.trips == trips
    assert card.hsd_fills == [Decimal(v) for v in hsd]


def test_date_without_year_is_the_latest_past_one():
    assert parse_card("CARD L11 28-12 1st 3", today=date(2027, 1, 2)).entry_date == date(2026, 12, 28)


@pytest.mark.parametrize(
    "text, error",
    [
        ("CARD\n1st 36", "Loader missing"),
        ("CARD L11\n1st 36", "Date missing"),
        ("CARD L11 06-11\n", "Nothing to save"),
        ("CARD L11 06-11\n1st: 36.5", "trips must be a whole number"),
        ("CARD L11 06-11\nHSD: abc", "HSD: unreadable number 'abc'"),
        ("CARD L11 06-11\nHSD:", "HSD: no litres"),
        ("CARD L11 06-11\n1st:", "1st shift: no trips number"),
        ("CARD L11 06-11\n1st 36\n1st 38", "1st shift given twice"),
        ("CARD L11 12-11-2026 1st 3", "in the future"),
        ("CARD L11 31-02-2026 1st 3", "is not a valid date"),
    ],
)
def test_errors_are_reported_not_guessed(text, error):
    with pytest.raises(CardParseError) as exc:
        parse_card(text, today=TODAY)
    assert any(error in e for e in exc.value.errors), exc.value.errors


def test_only_messages_starting_with_card_are_card_messages():
    assert not is_card_message("*LOADER REPORT - CHP*\n*DATE*:- 06/11/2026\n*SHIFT*:- 1st Shift")
    assert not is_card_message("Loader 11\nTrip card 36")  # "card" must be in the first line
    assert is_card_message("Trip card L11 06-11 1st 36")
