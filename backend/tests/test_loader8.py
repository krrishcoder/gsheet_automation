"""A loader kept as one row per day ("Loader 8": Date, HSD, HM, Total TRIP, AVG): 'L08 10-11 H 120 5355.2 T 110'."""

from datetime import date
from urllib.parse import unquote

import pytest

from tests.conftest import ADMIN_ID, Bot
from tests.fakes import FakeWorksheet, add_card_block, add_daily_table, add_refill_log, add_speed_block
from tests.test_multi_table import SHARED_ID, SHARED_URL, combined_tab

D8, D9, D10 = date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10)
DAYS = {D8: (160, 5331.1, 75), D9: (194, 5343.1, 108)}  # as in the real tab


def setup(container, *, quantity_header=False):
    container.today = lambda: D10
    shared, _ = combined_tab("side", title="Loader Report nov 2026")
    add_card_block(shared, top=41)
    add_speed_block(shared, top=41)
    add_refill_log(shared, top=41, entries=[(D9, 99.06, 4260.2), (D10, 132.4, 4276.2)])
    l8 = FakeWorksheet("Loader 8 nov 2026")
    rows = add_daily_table(l8, days=DAYS, quantity_header=quantity_header)
    container.sheets.add_spreadsheet(SHARED_ID, title="Loader11_data")
    container.sheets.add_worksheet(SHARED_ID, shared)
    container.sheets.add_worksheet(SHARED_ID, l8)
    bot = Bot(container)
    bot.send(f"/set_sheet 1 5 8 11 {SHARED_URL}", user_id=ADMIN_ID)
    return bot, l8, rows


def confirm(bot, text):
    summary = bot.send(text, user_id=ADMIN_ID)
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kc:"))
    before = len(bot.telegram.sent)
    bot.press(button, user_id=ADMIN_ID, message=summary)
    return summary, bot.telegram.sent[before:]


def row_values(ws, row):
    """(HSD, HM, Total TRIP, AVG) — columns C, D, E, I."""
    return tuple(ws.value(f"{c}{row}") for c in "CDEI")


def test_refill_and_trips_fill_the_days_row(container):
    bot, ws, rows = setup(container)
    r = rows[D10]
    assert r == 13  # same row as the real tab
    summary, sent = confirm(bot, "L08 10-11\nH 120 5355.2\nT 110")
    s = summary["text"]
    assert "Loader 8, row 13" in s
    assert "• Total trips: 110 (E13, new)" in s
    assert "• HSD (C13): adding 120 → 120 L" in s
    assert "• HM (D13): 5355.2, new" in s
    assert "• AVG (I13): =C13/(D13-D12) → 9.917 L/h" in s  # 120 L / (5355.2 − 5343.1)
    assert sent[-1]["text"].startswith("✅ <b>Saved</b>") and "• Refill 120 L at HM 5355.2, avg 9.917 L/h" in sent[-1]["text"]
    assert row_values(ws, r) == (120.0, 5355.2, 110, pytest.approx(9.917, abs=1e-3))
    assert ws.get(f"I{r}").formula == "=C13/(D13-D12)"

    again = bot.send("L08 10-11 H 120 5355.2 T 110", user_id=ADMIN_ID)["text"]
    assert "Nothing to change" in again and "120 L at HM 5355.2: already saved" in again


def test_second_refill_the_same_day(container):
    bot, ws, rows = setup(container)
    confirm(bot, "L08 10-11 H 120 5355.2")
    summary, _ = confirm(bot, "L08 10-11 H 60 5362")
    assert "• HM (D13): 5362, was 5355.2" in summary["text"]
    r = rows[D10]
    assert ws.get(f"C{r}").formula == "=120+60" and ws.value(f"D{r}") == 5362.0
    assert ws.get(f"I{r}").formula == "=C13/(D13-D12)"  # kept: it already covers the whole day


def test_checks(container):
    bot, ws, rows = setup(container)
    assert "Refill at HM 5340 is not after the previous refill (HM 5343.1, row 12)" in bot.send(
        "L08 10-11 H 90 5340", user_id=ADMIN_ID
    )["text"]
    assert "Loader 08 keeps one 'Total TRIP' per day — send 'T 110' instead of T1/T2/T3" in bot.send(
        "L08 10-11 T1 50", user_id=ADMIN_ID
    )["text"]
    assert "Loader 11 keeps trips per shift — send T1, T2, T3 instead of 'T'" in bot.send("L11 10-11 T 110", user_id=ADMIN_ID)["text"]
    assert "has no opening/closing HM block for Loader 08" in bot.send("L08 10-11 O1 5343", user_id=ADMIN_ID)["text"]
    assert container.sheets.writes == []


@pytest.mark.parametrize("quantity_header", [False, True])
def test_boss_summary_has_loader_8_and_11_together(make_container, quantity_header):
    container = make_container(summary_after_update=True)
    bot, ws, rows = setup(container, quantity_header=quantity_header)
    bot.send("/summary_loaders 8 11", user_id=ADMIN_ID)
    _, sent = confirm(bot, "L08 10-11 H 120 5355.2 T 110")
    month = sent[-1]
    assert month["text"].startswith("📊")
    text = unquote(month["reply_markup"]["inline_keyboard"][0][0]["url"].split("?text=", 1)[1])
    l8, l11 = text.split("*Loader 08*", 1)[1].split("*Loader 11*", 1)
    assert l8.startswith(" (till 10-11)\nTrips: 293\n")  # 75 + 108 + 110
    # only with a header on the =trips*30 column (the fake sheet does not recalculate 10-11's =E13*30, as Sheets would)
    assert ("Quantity: 5,490.00 MT" in l8) == quantity_header
    # month: (160 + 194 + 120) L / 293 trips; latest day: 09-11's 194 / 108 (the fake does not recalculate 10-11's formula)
    assert "Per trip avg: 1.62 L/trip (month), 1.80 on 09-11" in l8
    assert "Diesel avg: 9.917 L/h (last refill, HM 5355.2)" in l8
    assert "Per trip avg" not in l11  # only for a loader kept as one row per day
    assert "Net operating profit: ₹72,347.06" in l8 and "Profit with GST: ₹118,842.41" in l8
    assert "Diesel avg: 8.275 L/h (last refill, HM 4276.2)" in l11  # Loader 11 is in the same message
    assert "*Loader 01*" not in text
