"""Month summary for the boss, sent after each sheet update with a WhatsApp click-to-chat button."""

from datetime import date
from urllib.parse import unquote

import pytest

from app.services.summary_service import normalize_phone, whatsapp_link
from tests.conftest import ADMIN_ID, SAMPLE_REPORT, USER_ID, Bot
from tests.fakes import add_card_block, add_hm_block, add_refill_log, add_speed_block, add_statement
from tests.test_multi_table import SHARED_ID, SHARED_URL, combined_tab

D9, D10 = date(2026, 11, 9), date(2026, 11, 10)
LOG = [(date(2026, 11, 8), 119.75, 4254.4), (D9, 99.06, 4260.2), (D9, 121.11, 4268.0), (D10, 132.4, 4276.2)]


@pytest.fixture
def boss_bot(make_container):
    container = make_container(summary_after_update=True)
    container.today = lambda: D10
    ws, _ = combined_tab("side", title="Loader Report nov 2026")
    add_card_block(ws, top=41)
    add_speed_block(ws, top=41, speeds={D9: (7.5, 0, 6.2), D10: (8.432, 0, "#DIV/0!")})
    add_hm_block(ws, top=41)
    add_refill_log(ws, top=41, entries=LOG)
    add_statement(ws)
    container.sheets.add_spreadsheet(SHARED_ID, title="CHP loaders")
    container.sheets.add_worksheet(SHARED_ID, ws)
    bot = Bot(container)
    bot.send(f"/set_sheet 1 5 11 {SHARED_URL}", user_id=ADMIN_ID)
    return bot


def button_urls(message):
    return [b["url"] for row in (message.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def sent_text(url):
    return unquote(url.split("?text=", 1)[1])


def test_summary_follows_a_report_update(boss_bot):
    bot = boss_bot
    summary = bot.send(SAMPLE_REPORT, user_id=ADMIN_ID)
    bot.confirm(summary, user_id=ADMIN_ID)
    result, month = bot.telegram.sent[-2:]
    assert result["text"].startswith("✅ SUCCESS")
    assert month["text"].startswith("📊 <b>Month summary</b>") and month["reply_to"] == result["message_id"]

    (url,) = button_urls(month)
    assert url.startswith("https://wa.me/?text=")  # no boss number yet: WhatsApp asks whom to send to
    text = sent_text(url)
    assert text.startswith("*Loader summary · November 2026*\nUpdated: 06-11 1st Shift\n")
    assert "*Loader 01* (till 06-11)\nTrips: 42\nQuantity: 1,340.84 MT\n" in text
    assert "*Loader 05* (till 06-11)\nTrips: 43\nQuantity: 1,411.93 MT" in text
    loader11 = text.split("*Loader 11*", 1)[1]
    # Loader 11 has a card block: the WhatsApp report's and the trip card's figures, side by side
    assert loader11.startswith(" (till 06-11)\nWhatsApp report: 21 trips · 696.38 MT\nCard details: no entries yet\n")
    assert "Diesel avg: 16.146 L/h (last refill, HM 4276.2)" in loader11  # the latest refill, not a month average
    assert "Speed: 8.432 min/trip (10-11, 1st shift)" in loader11  # 3rd shift #DIV/0! and 2nd shift 0 skipped
    assert "Net operating profit: ₹71,514.61" in loader11 and "Profit with GST: ₹124,606.06" in loader11
    assert "24.25%" not in text  # the margin is not the profit
    assert "Diesel" not in text.split("*Loader 11*")[0]  # loaders 01/05 have no refill log: line left out


def test_boss_number_opens_his_chat(boss_bot):
    bot = boss_bot
    assert "+919876543210" in bot.send("/boss_add 98765 43210 Sharma ji", user_id=ADMIN_ID)["text"]
    assert "⛔" in bot.send("/boss_add 9876543210", user_id=USER_ID)["text"]  # admins only
    assert "Sharma ji <code>+919876543210</code>" in bot.send("/boss_list", user_id=ADMIN_ID)["text"]

    summary = bot.send("L11 10-11 T1 37", user_id=ADMIN_ID)
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kc:"))
    bot.press(button, user_id=ADMIN_ID, message=summary)
    month = bot.telegram.sent[-1]
    assert month["text"].startswith("📊")
    keyboard = month["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["text"] == "📲 Send to Sharma ji on WhatsApp"
    url = keyboard[0][0]["url"]
    assert url.startswith("https://wa.me/919876543210?text=")
    text = sent_text(url)
    assert "Updated: 10-11 card entry" in text and "*Loader 11* (till 06-11)" not in text  # card trips are not CHP trips
    assert "*Loader 01*" not in text  # only the loader that was updated

    assert "Removed" in bot.send("/boss_remove +91 98765 43210", user_id=ADMIN_ID)["text"]
    assert "No WhatsApp numbers yet" in bot.send("/boss_list", user_id=ADMIN_ID)["text"]


def test_summary_command(boss_bot):
    bot = boss_bot
    bot.confirm(bot.send(SAMPLE_REPORT, user_id=ADMIN_ID), user_id=ADMIN_ID)
    reply = bot.send("/summary 11", user_id=ADMIN_ID)
    assert reply["text"].startswith("📊") and "Loader 11" in reply["text"] and "Loader 01" not in reply["text"]
    assert button_urls(reply)
    assert "Loader 01" in bot.send("/summary", user_id=ADMIN_ID)["text"]  # admin: every loader
    bot.send(f"/grant_loader {USER_ID} 5", user_id=ADMIN_ID)
    mine = bot.send("/summary", user_id=USER_ID)["text"]
    assert "Loader 05" in mine and "Loader 11" not in mine  # a user: only his loaders
    assert "no access" in bot.send("/summary 11", user_id=USER_ID)["text"]


def test_nothing_is_summarised_when_turned_off(make_container):
    container = make_container()  # summary_after_update off
    assert not container.settings.summary_after_update


@pytest.mark.parametrize(
    "raw, phone",
    [("9876543210", "919876543210"), ("+91 98765 43210", "919876543210"), ("098765-43210", "919876543210"),
     ("+44 7700 900123", "447700900123"), ("12345", None), ("abc", None)],
)
def test_phone_numbers(raw, phone):
    assert normalize_phone(raw) == phone


def test_whatsapp_link_encodes_the_whole_text():
    url = whatsapp_link("*Loader 11*\nProfit: ₹71,514.61 & more", "919876543210")
    assert url.startswith("https://wa.me/919876543210?text=") and "\n" not in url and " " not in url and "&" not in url.split("?", 1)[1]
    assert sent_text(url) == "*Loader 11*\nProfit: ₹71,514.61 & more"


def test_summary_limited_to_chosen_loaders(boss_bot):
    bot = boss_bot
    assert "covers only <b>Loader 11</b>" in bot.send("/summary_loaders 11", user_id=ADMIN_ID)["text"]
    assert "Not registered: Loader 07" in bot.send("/summary_loaders 7", user_id=ADMIN_ID)["text"]
    assert "⛔" in bot.send("/summary_loaders all", user_id=USER_ID)["text"]  # admins only

    bot.confirm(bot.send(SAMPLE_REPORT, user_id=ADMIN_ID), user_id=ADMIN_ID)  # writes loaders 01, 05 and 11
    month = bot.telegram.sent[-1]
    text = sent_text(button_urls(month)[0])
    assert "*Loader 11*" in text and "*Loader 01*" not in text and "*Loader 05*" not in text
    assert "Loader 01" not in month["text"]

    on_demand = bot.send("/summary", user_id=ADMIN_ID)["text"]
    assert "Loader 11" in on_demand and "Loader 01" not in on_demand
    assert "Loader 01" in bot.send("/summary 1", user_id=ADMIN_ID)["text"]  # asking by number still works
    bot.send("/boss_add 9876543210 Boss", user_id=ADMIN_ID)
    assert "Covers: Loader 11 (/summary_loaders)" in bot.send("/boss_list", user_id=ADMIN_ID)["text"]

    bot.send("/summary_loaders 5", user_id=ADMIN_ID)
    before = len(bot.telegram.sent)
    summary = bot.send("L11 10-11 T1 37", user_id=ADMIN_ID)
    button = next(d for d in bot.telegram.buttons(summary) if d.startswith("kc:"))
    bot.press(button, user_id=ADMIN_ID, message=summary)
    assert not any(m["text"].startswith("📊") for m in bot.telegram.sent[before:])  # Loader 11 not covered: no summary

    assert "covers <b>every loader</b>" in bot.send("/summary_loaders all", user_id=ADMIN_ID)["text"]
    assert "Loader 01" in bot.send("/summary", user_id=ADMIN_ID)["text"]


def test_card_figures_next_to_the_report(boss_bot):
    bot = boss_bot
    ws = bot.sheets.tab(SHARED_ID, "Loader Report nov 2026")
    bot.confirm(bot.send(SAMPLE_REPORT, user_id=ADMIN_ID), user_id=ADMIN_ID)  # report: 21 trips, 696.38 MT on 06-11
    for ref, value in (("F48", 36), ("G48", 37), ("F49", 30), ("J48", 1200.0), ("J49", 500.0)):
        ws.set(ref, value)  # card: 06-11 36 + 37 trips, 07-11 30; Est. Weight 1200 + 500
    text = bot.send("/summary 11", user_id=ADMIN_ID)["text"]
    assert "WhatsApp report: 21 trips · 696.38 MT\nCard details: 103 trips · 1,700.00 MT (till 07-11)" in text
    ws.set("D73", "Total")
    ws.set("J73", 1690.25, formula="=W35*P73")  # the sheet's own total (avg weight × trips) wins over the sum
    assert "Card details: 103 trips · 1,690.25 MT (till 07-11)" in bot.send("/summary 11", user_id=ADMIN_ID)["text"]
