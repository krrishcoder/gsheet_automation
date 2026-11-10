"""Card messages written to the loader's blocks: Card Details, Opening and Closing, hsd avg."""

from datetime import date

import sqlalchemy as sa
from sqlalchemy import select

from app.database.database import Database
from app.database.models import CardEntry, CardStatus
from tests.conftest import ADMIN_ID, REPORT_DATE, SAMPLE_REPORT, USER_ID, add_loader
from tests.fakes import add_card_block, add_hm_block, add_refill_log, serial
from tests.test_multi_table import SHARED_ID, SHARED_URL, combined_tab

DAY = "06-11-2026"
D9 = date(2026, 11, 9)
# the real log before the bot: 08/11 119.75 L at 4254.4, 09/11 99.06 at 4260.2 and 121.11 at 4268, 10/11 …
LOG = [(date(2026, 11, 8), 119.75, 4254.4), (D9, 99.06, 4260.2), (D9, 121.11, 4268.0)]


def setup_shared(container, bot, *, log=LOG):
    """The real layout: three loader tables side by side, Loader 11's blocks below them
    (Card Details at D, Opening and Closing at AE, hsd avg at AO, all titled in row 41)."""
    container.today = lambda: date(2026, 11, 9)
    ws, tables = combined_tab("side", title="Loader Report nov 2026")
    card_rows = add_card_block(ws, top=41)
    add_hm_block(ws, top=41)
    add_refill_log(ws, top=41, entries=log)
    container.sheets.add_spreadsheet(SHARED_ID, title="CHP loaders")
    container.sheets.add_worksheet(SHARED_ID, ws)
    bot.send(f"/set_sheet 1 5 11 {SHARED_URL}", user_id=ADMIN_ID)
    return ws, card_rows[REPORT_DATE]


def card(ws, row):
    """(HSD, 1st, 2nd, 3rd) of the card row (columns E–H)."""
    return tuple(ws.value(f"{c}{row}") for c in "EFGH")


def hm(ws, row):
    """Opening/closing HM of the three shifts (columns AF–AK)."""
    return tuple(ws.value(f"{c}{row}") for c in ("AF", "AG", "AH", "AI", "AJ", "AK"))


def send_and_confirm(bot, text, *, user_id=ADMIN_ID):
    summary = bot.send(text, user_id=user_id)
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kc:"))
    before = len(bot.telegram.sent)
    bot.press(button, user_id=user_id, message=summary)
    return summary, bot.telegram.sent[before:][-1]


# --- Short codes: all three blocks -----------------------------------------------------------------


def test_short_codes_fill_card_hm_and_refill_log(bot, container):
    ws, _ = setup_shared(container, bot)
    row = 51  # 09-11, as in the real sheet
    text = "L11 09-11\nT1 37 O1 4268 C1 4272\nT2 35 O2 4272 C2 4276.2\nH 120 4276.2"
    summary, result = send_and_confirm(bot, text)
    s = summary["text"]
    assert "Trips — Loader 11 Card Details, row 51\n• 1st: 37 (F51, new)\n• 2nd: 35 (G51, new)" in s
    assert "Hour meter — Loader 11 Opening and Closing, row 51" in s
    assert "• 1st: opening 4268 (AF51, new) · closing 4272 (AG51, new) → 4 h" in s
    assert "• 2nd: opening 4272 (AH51, new) · closing 4276.2 (AI51, new) → 4.2 h" in s
    # the log's last refill is at HM 4268 (row 45): 120 / (4276.2 - 4268)
    assert "Diesel refills — Loader 11 hsd avg\n• 120 L at HM 4276.2 → new row 46 · avg 14.634 L/h" in s
    assert "HSD Fuel (E51): adding 120 → 120 L" in s
    assert "⚠️" not in s  # shifts under 8 h, each opening = the previous closing

    assert card(ws, row) == (120.0, 37, 35, None)
    assert hm(ws, row) == (4268.0, 4272.0, 4272.0, 4276.2, None, None)
    # new log row: a real date (shown dd-mm-yyyy), litres, HM and the same avg formula as the typed rows
    assert ws.value("AO46") == serial(D9) and ws.get("AO46").formatted == "09-11-2026"
    assert (ws.value("AQ46"), ws.value("AR46")) == (120.0, 4276.2)
    assert ws.get("AS46").formula == "=AQ46/(AR46-AR45)" and round(ws.value("AS46"), 3) == 14.634
    assert result["text"].startswith("✅ <b>Saved</b> card #")
    assert "• Refill 120 L at HM 4276.2 → row 46, avg 14.634 L/h" in result["text"]
    assert "• 1st shift opening HM 4268 (AF51)" in result["text"]

    # everything again: nothing is added twice
    again = bot.send(text, user_id=ADMIN_ID)
    assert "Nothing to change" in again["text"] and "120 L at HM 4276.2: already logged" in again["text"]
    assert ws.value("AO47") is None


def test_one_value_at_a_time(bot, container):
    ws, _ = setup_shared(container, bot)
    send_and_confirm(bot, "L11 09-11 C3 4291.5")
    assert hm(ws, 51) == (None, None, None, None, None, 4291.5)
    assert card(ws, 51) == (None, None, None, None)  # no other block touched
    send_and_confirm(bot, "L11 09-11 H 50 4290")
    assert (ws.value("AQ46"), ws.value("AR46")) == (50.0, 4290.0)
    assert card(ws, 51)[0] == 50.0  # the refill's litres also go to HSD Fuel


def test_hm_checks(bot, container):
    ws, _ = setup_shared(container, bot)
    blocked = bot.send("L11 09-11 O1 4268 C1 4260", user_id=ADMIN_ID)["text"]
    assert "Not saved: 1st shift: closing HM 4260 is below opening HM 4268" in blocked

    warned = bot.send("L11 09-11 O1 4268 C1 4280\nO2 4281", user_id=ADMIN_ID)
    assert "⚠️ 1st shift ran 12 h by the hour meter (more than 8)" in warned["text"]
    assert "⚠️ 2nd shift opening HM 4281 is not the 1st shift's closing HM 4280" in warned["text"]
    assert any(b.startswith("kc:") for b in bot.telegram.buttons(warned))  # warnings can still be confirmed

    ws.set("AK50", 4266)  # 08-11 3rd shift closed at 4266
    shift1 = bot.send("L11 09-11 O1 4268", user_id=ADMIN_ID)["text"]
    assert "1st shift opening HM 4268 is not the previous day's 3rd-shift closing HM 4266" in shift1

    backwards = bot.send("L11 09-11 H 80 4265", user_id=ADMIN_ID)["text"]
    assert "Refill at HM 4265 is not after the last refill (HM 4268, row 45)" in backwards
    assert container.sheets.writes == []


def test_first_refill_of_an_empty_log_has_no_avg(bot, container):
    ws, _ = setup_shared(container, bot, log=[])
    summary, _ = send_and_confirm(bot, "L11 09-11 H 119.75 4254.4")
    assert "• 119.75 L at HM 4254.4 → new row 43 · avg: no earlier refill to compare" in summary["text"]
    assert ws.value("AS43") is None
    send_and_confirm(bot, "L11 09-11 H 99.06 4260.2")
    assert ws.get("AS44").formula == "=AQ44/(AR44-AR43)"


def test_hm_block_missing_is_explained(bot, container):
    container.today = lambda: date(2026, 11, 9)
    ws, _ = add_loader(container, 11)
    add_card_block(ws, top=40, left=2)
    text = bot.send("L11 09-11 O1 4268", user_id=USER_ID)["text"]
    assert "has no opening/closing HM block for Loader 11" in text and "'Loader 11 Opening and Closing'" in text


# --- Trips and HSD Fuel (long form still accepted) -------------------------------------------------


def test_day_filled_shift_by_shift_with_hsd_fills(bot, container):
    ws, row = setup_shared(container, bot)
    assert row == 48  # same cell positions as the real sheet

    summary, result = send_and_confirm(bot, f"CARD L11\nDate: {DAY}\n1st: 36")
    assert "• 1st: 36 (F48, new)" in summary["text"]
    assert "• 1st shift trips 36 (F48)" in result["text"]
    assert card(ws, row) == (None, 36, None, None)  # only the sent shift

    send_and_confirm(bot, "CARD L11 06-11 3rd 44 HSD 154.98")
    assert card(ws, row) == (154.98, 36, None, 44)
    assert ws.get(f"E{row}").formula is None  # a single fill stays a plain number

    summary, _ = send_and_confirm(bot, f"CARD L11 {DAY}\n2nd 37\nHSD 190 + 154.98")
    assert "HSD Fuel (E48): adding 190, 154.98 already in it → 154.98 + 190 = 344.98 L" in summary["text"]
    assert card(ws, row) == (344.98, 36, 37, 44)
    assert ws.get(f"E{row}").formula == "=154.98+190"  # fills kept visible, like the hand-typed cells
    assert ws.value(f"I{row}") == 0 and ws.get(f"I{row}").formula  # Total Trips formula untouched

    again = bot.send(f"CARD L11 {DAY}\n2nd 37\nHSD 190 + 154.98", user_id=ADMIN_ID)
    assert "Nothing to change" in again["text"] and not bot.telegram.buttons(again)

    assert container.sheets.write_modes == [True, True, True]
    for _, _, writes in container.sheets.writes:
        assert {(w.row, w.col) for w in writes} <= {(row, c) for c in range(5, 9)}


def test_card_is_compared_with_the_chp_report(bot, container):
    setup_shared(container, bot)
    bot.confirm(bot.send(SAMPLE_REPORT, user_id=ADMIN_ID), user_id=ADMIN_ID)  # Loader 11 1st shift: 21 trips
    summary = bot.send("L11 06-11 T1 36 T2 37", user_id=ADMIN_ID)
    assert "• 1st: 36 (F48, new) · CHP report 21 (card +15)" in summary["text"]
    assert "• 2nd: 37 (G48, new)\n" in summary["text"]  # no CHP figure yet for the 2nd shift


def test_filled_cell_is_replaced_only_after_seeing_the_change(bot, container):
    ws, row = setup_shared(container, bot)
    ws.set(f"F{row}", 30)
    ws.set(f"AF{row}", 4200)
    summary, _ = send_and_confirm(bot, "L11 06-11 T1 36 O1 4210")
    assert "• 1st: 36 (F48, ⚠️ replaces 30)" in summary["text"]
    assert "opening 4210 (AF48, ⚠️ replaces 4200)" in summary["text"]
    assert card(ws, row)[1] == 36 and ws.value(f"AF{row}") == 4210.0


def test_sheet_edited_between_preview_and_confirm_is_a_conflict(bot, container):
    ws, row = setup_shared(container, bot)
    summary = bot.send("L11 06-11 T3 44 H 120 4284", user_id=ADMIN_ID)
    ws.set(f"H{row}", 40)  # someone typed into the cell meanwhile
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kc:"))
    bot.press(button, user_id=ADMIN_ID, message=summary)
    result = bot.telegram.last(ADMIN_ID)["text"]
    assert "not saved" in result and "H48 changed since the preview (was empty, now 40)" in result
    assert ws.value(f"H{row}") == 40 and ws.value("AQ46") is None  # nothing written at all


def test_cancel_writes_nothing(bot, container):
    ws, row = setup_shared(container, bot)
    summary = bot.send("L11 06-11 T1 36", user_id=ADMIN_ID)
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kx:"))
    bot.press(button, user_id=ADMIN_ID, message=summary)
    assert card(ws, row) == (None, None, None, None)
    with container.db.session() as s:
        assert s.scalar(select(CardEntry.status)) == CardStatus.CANCELLED
    assert "Cancelled by" in bot.telegram.edits[-1]["text"]


def test_blocked_cases(bot, container):
    ws, row = setup_shared(container, bot)
    assert "No spreadsheet configured for Loader 07" in bot.send("L07 06-11 T1 36", user_id=ADMIN_ID)["text"]
    no_block = bot.send("L05 06-11 T1 36", user_id=ADMIN_ID)["text"]
    assert "has no card block for Loader 05" in no_block and "'Loader 05 Card Details'" in no_block

    bot.send(f"/grant_loader {USER_ID} 1", user_id=ADMIN_ID)
    assert "not authorised for Loader 11" in bot.send("L11 06-11 T1 36", user_id=USER_ID)["text"]

    ws.set(f"E{row}", 0, formula=f"=C{row}*2")
    hsd = bot.send(f"CARD L11 {DAY} HSD 190", user_id=ADMIN_ID)["text"]
    assert "E48 has a formula (=C48*2) that is not a sum of litres" in hsd
    assert container.sheets.writes == []


def test_unreadable_card_gets_the_format(bot, container):
    setup_shared(container, bot)
    reply = bot.send("L11\nT1 36 O1 42 68", user_id=ADMIN_ID)["text"]
    assert "Could not read this card message" in reply and "Date missing" in reply and "Could not read '68'" in reply
    assert "L11 10-11\nT1 37 O1 4268 C1 4272" in reply
    assert "<code>H 132.4 4276.2</code> diesel refill" in bot.send("/card_format", user_id=USER_ID)["text"]


def test_card_block_in_a_loader_tab(bot, container):
    container.today = lambda: date(2026, 11, 9)
    ws, rows = add_loader(container, 11)  # tab "Loader 11 nov 2026" with its shift table
    card_rows = add_card_block(ws, top=40, left=2)
    send_and_confirm(bot, f"CARD L11 {DAY}\n1st 36\n2nd 37\n3rd 44\nHSD 190 + 154.98", user_id=USER_ID)
    r = card_rows[REPORT_DATE]
    assert tuple(ws.value(f"{c}{r}") for c in "CDEF") == (344.98, 36, 37, 44)
    assert ws.value(f"D{rows[REPORT_DATE]}") is None  # the shift table's 1st-shift trips: untouched


def test_new_card_columns_are_added_to_an_existing_table(tmp_path):
    db = Database(f"sqlite:///{tmp_path}/old.db")
    db.create_all()
    with db.engine.begin() as conn:  # the table as the previous version created it
        conn.exec_driver_sql("ALTER TABLE card_entries DROP COLUMN refills")
        conn.exec_driver_sql("ALTER TABLE card_entries DROP COLUMN hm_readings")
    db.dispose()
    db = Database(f"sqlite:///{tmp_path}/old.db")
    db.create_all()
    columns = {c["name"] for c in sa.inspect(db.engine).get_columns("card_entries")}
    assert {"refills", "hm_readings"} <= columns
    db.dispose()
