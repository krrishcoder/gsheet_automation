"""Typed trip-card / HSD messages written to the loader's "Card Details" block."""

from datetime import date

from sqlalchemy import select

from app.database.models import CardEntry, CardStatus
from tests.conftest import ADMIN_ID, REPORT_DATE, SAMPLE_REPORT, USER_ID, add_loader
from tests.fakes import add_card_block
from tests.test_multi_table import SHARED_ID, SHARED_URL, combined_tab

DAY = "06-11-2026"


def setup_shared(container, bot):
    """The real layout: three loader tables side by side, Loader 11's card block below them."""
    container.today = lambda: date(2026, 11, 9)
    ws, tables = combined_tab("side", title="Loader Report nov 2026")
    card_rows = add_card_block(ws, top=41)
    container.sheets.add_spreadsheet(SHARED_ID, title="CHP loaders")
    container.sheets.add_worksheet(SHARED_ID, ws)
    bot.send(f"/set_sheet 1 5 11 {SHARED_URL}", user_id=ADMIN_ID)
    return ws, card_rows[REPORT_DATE]


def card(ws, row):
    """(HSD, 1st, 2nd, 3rd) of the card row (columns E–H)."""
    return tuple(ws.value(f"{c}{row}") for c in "EFGH")


def send_and_confirm(bot, text, *, user_id=ADMIN_ID):
    summary = bot.send(text, user_id=user_id)
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kc:"))
    before = len(bot.telegram.sent)
    bot.press(button, user_id=user_id, message=summary)
    return summary, bot.telegram.sent[before:][-1]


def test_day_filled_shift_by_shift_with_hsd_fills(bot, container):
    ws, row = setup_shared(container, bot)
    assert row == 48  # same cell positions as the real sheet

    summary, result = send_and_confirm(bot, f"CARD L11\nDate: {DAY}\n1st: 36")
    assert "1st Shift: 36 trips (F48, new)" in summary["text"]
    assert "Day after saving: 1st 36 · 2nd – · 3rd – · HSD –" in summary["text"]
    assert result["text"].startswith("✅ <b>Saved</b>") and "• 1st Shift: 36 trips (F48)" in result["text"]
    assert card(ws, row) == (None, 36, None, None)  # only the sent shift

    send_and_confirm(bot, "CARD L11 06-11 3rd 44 HSD 154.98")
    assert card(ws, row) == (154.98, 36, None, 44)
    assert ws.get(f"E{row}").formula is None  # a single fill stays a plain number

    summary, _ = send_and_confirm(bot, f"CARD L11 {DAY}\n2nd 37\nHSD 190 + 154.98")
    assert "HSD (E48): adding 190, 154.98 already saved → 154.98 + 190 = 344.98 L" in summary["text"]
    assert card(ws, row) == (344.98, 36, 37, 44)
    assert ws.get(f"E{row}").formula == "=154.98+190"  # fills kept visible, like the hand-typed cells
    assert ws.value(f"I{row}") == 0 and ws.get(f"I{row}").formula  # Total Trips formula untouched

    again = bot.send(f"CARD L11 {DAY}\n2nd 37\nHSD 190 + 154.98", user_id=ADMIN_ID)
    assert "Nothing to change" in again["text"] and not bot.telegram.buttons(again)

    # only the card writes used USER_ENTERED (formulas); nothing outside E–H was written
    assert container.sheets.write_modes == [True, True, True]
    for _, _, writes in container.sheets.writes:
        assert {(w.row, w.col) for w in writes} <= {(row, c) for c in range(5, 9)}


def test_card_is_compared_with_the_chp_report(bot, container):
    setup_shared(container, bot)
    bot.confirm(bot.send(SAMPLE_REPORT, user_id=ADMIN_ID), user_id=ADMIN_ID)  # Loader 11 1st shift: 21 trips
    summary = bot.send(f"CARD L11\nDate: {DAY}\n1st: 36\n2nd: 37", user_id=ADMIN_ID)
    assert "1st Shift: 36 trips (F48, new) · CHP report 21 (card +15)" in summary["text"]
    assert "2nd Shift: 37 trips (G48, new)" in summary["text"]  # no CHP figure yet for the 2nd shift


def test_filled_shift_is_replaced_only_after_seeing_the_change(bot, container):
    ws, row = setup_shared(container, bot)
    ws.set(f"F{row}", 30)
    summary, _ = send_and_confirm(bot, f"CARD L11 {DAY} 1st 36")
    assert "1st Shift: 36 trips (F48, ⚠️ replaces 30)" in summary["text"]
    assert card(ws, row)[1] == 36


def test_sheet_edited_between_preview_and_confirm_is_a_conflict(bot, container):
    ws, row = setup_shared(container, bot)
    summary = bot.send(f"CARD L11 {DAY} 3rd 44", user_id=ADMIN_ID)
    ws.set(f"H{row}", 40)  # someone typed into the cell meanwhile
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kc:"))
    bot.press(button, user_id=ADMIN_ID, message=summary)
    result = bot.telegram.last(ADMIN_ID)["text"]
    assert "not saved" in result and "H48 changed since the preview (was empty, now 40)" in result
    assert ws.value(f"H{row}") == 40


def test_cancel_writes_nothing(bot, container):
    ws, row = setup_shared(container, bot)
    summary = bot.send(f"CARD L11 {DAY} 1st 36", user_id=ADMIN_ID)
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kx:"))
    bot.press(button, user_id=ADMIN_ID, message=summary)
    assert card(ws, row) == (None, None, None, None)
    with container.db.session() as s:
        assert s.scalar(select(CardEntry.status)) == CardStatus.CANCELLED
    assert "Cancelled by" in bot.telegram.edits[-1]["text"]


def test_blocked_cases(bot, container):
    ws, row = setup_shared(container, bot)
    text = lambda n: f"CARD L{n:02d} {DAY} 1st 36"  # noqa: E731

    assert "No spreadsheet configured for Loader 07" in bot.send(text(7), user_id=ADMIN_ID)["text"]
    no_block = bot.send(text(5), user_id=ADMIN_ID)["text"]
    assert "has no card block for Loader 05" in no_block and "'Loader 05 Card Details'" in no_block

    bot.send(f"/grant_loader {USER_ID} 1", user_id=ADMIN_ID)
    assert "not authorised for Loader 11" in bot.send(text(11), user_id=USER_ID)["text"]

    ws.set(f"E{row}", 0, formula=f"=C{row}*2")
    hsd = bot.send(f"CARD L11 {DAY} HSD 190", user_id=ADMIN_ID)["text"]
    assert "E48 has a formula (=C48*2) that is not a sum of litres" in hsd
    assert container.sheets.writes == []


def test_unreadable_card_gets_the_format(bot, container):
    setup_shared(container, bot)
    reply = bot.send("CARD L11\n1st: 36", user_id=ADMIN_ID)["text"]
    assert "Could not read this trip card" in reply and "Date missing" in reply
    assert "CARD L11\nDate: 06-11-2026\n1st: 36" in reply
    assert "CARD L11" in bot.send("/card_format", user_id=USER_ID)["text"]


def test_card_block_in_a_loader_tab(bot, container):
    container.today = lambda: date(2026, 11, 9)
    ws, rows = add_loader(container, 11)  # tab "Loader 11 nov 2026" with its shift table
    card_rows = add_card_block(ws, top=40, left=2)
    send_and_confirm(bot, f"CARD L11 {DAY}\n1st 36\n2nd 37\n3rd 44\nHSD 190 + 154.98", user_id=USER_ID)
    r = card_rows[REPORT_DATE]
    assert tuple(ws.value(f"{c}{r}") for c in "CDEF") == (344.98, 36, 37, 44)
    assert ws.value(f"D{rows[REPORT_DATE]}") is None  # the shift table's 1st-shift trips: untouched
