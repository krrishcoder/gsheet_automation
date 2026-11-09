from sqlalchemy import select

from app.database.models import AuditLog, Loader, LoaderWorksheet, User
from tests.conftest import ADMIN_ID, OTHER_USER_ID, REPORT_DATE, SAMPLE_REPORT, USER_ID, add_loader, sheet_id
from tests.fakes import build_month_sheet

URL = f"https://docs.google.com/spreadsheets/d/{sheet_id(11)}/edit#gid=0"


def test_admin_commands_require_admin(bot, container):
    add_loader(container, 11)
    reply = bot.send(f"/set_sheet 12 {URL}", user_id=USER_ID)
    assert "requires administrator" in reply["text"]
    with container.db.session() as s:
        assert s.scalars(select(Loader).where(Loader.loader_number == 12)).first() is None
        assert s.scalars(select(AuditLog).where(AuditLog.action == "admin_command_denied")).first() is not None


def test_set_sheet_validates_url_and_checks_access(bot, container):
    assert "Not a Google Sheets URL" in bot.send("/set_sheet 11 https://example.com/x", user_id=ADMIN_ID)["text"]
    reply = bot.send(f"/set_sheet 11 {URL}", user_id=ADMIN_ID)
    assert "not accessible yet" in reply["text"]  # not shared with the service account yet
    container.sheets.add_worksheet(sheet_id(11), build_month_sheet("Loader 11 nov 2026", 2026, 11)[0])
    reply = bot.send(f"/set_sheet loader-11 {URL}", user_id=ADMIN_ID)
    assert "already uses this spreadsheet" in reply["text"] and "Access OK" in reply["text"]
    assert "'Loader 11 nov 2026'" in reply["text"]
    with container.db.session() as s:
        loader = s.scalars(select(Loader)).one()
        assert (loader.loader_number, loader.spreadsheet_id) == (11, sheet_id(11))


def test_set_worksheet_with_quoted_name_and_smart_quotes(bot, container):
    add_loader(container, 11, worksheet=None)
    container.sheets.add_worksheet(sheet_id(11), build_month_sheet("Loader 11 nov 2026", 2026, 11)[0])
    reply = bot.send('/set_worksheet 11 2026 11 “Loader 11 nov 2026”', user_id=ADMIN_ID)
    assert "November 2026 → 'Loader 11 nov 2026'" in reply["text"]
    assert "No tab with exactly this name" not in reply["text"]
    reply = bot.send("/set_worksheet 11 2026 dec Loader 11 dec 2026", user_id=ADMIN_ID)
    assert "No tab with exactly this name" in reply["text"]
    with container.db.session() as s:
        names = {(m.month, m.worksheet_name) for m in s.scalars(select(LoaderWorksheet))}
    assert names == {(11, "Loader 11 nov 2026"), (12, "Loader 11 dec 2026")}
    listing = bot.send("/list_worksheets 11", user_id=ADMIN_ID)["text"]
    assert "December 2026 → 'Loader 11 dec 2026'" in listing
    assert "Usage" in bot.send("/set_worksheet 11 2026 13 x", user_id=ADMIN_ID)["text"]


def test_grant_and_revoke_loader_access(bot, container):
    add_loader(container, 11, users=())
    add_loader(container, 12, users=())
    reply = bot.send(f"/grant_loader {OTHER_USER_ID} 11", user_id=ADMIN_ID)
    assert "may now report for: Loader 11" in reply["text"]
    assert "Loader 11" in bot.send("/my_loaders", user_id=OTHER_USER_ID)["text"]

    # Loader 11 access does not extend to Loader 12.
    summary = bot.send(SAMPLE_REPORT + "\n*LOADER - 12*\nTrip:- 1 Qty:- 30", user_id=OTHER_USER_ID)
    assert "not authorised for Loader 12" in summary["text"]

    bot.send(f"/revoke_loader {OTHER_USER_ID} 11", user_id=ADMIN_ID)
    summary = bot.send(SAMPLE_REPORT, user_id=OTHER_USER_ID)
    assert "not authorised for Loader 11" in summary["text"]


def test_disable_loader_and_user(bot, container):
    add_loader(container, 11)
    bot.send("/disable_loader 11", user_id=ADMIN_ID)
    assert "Loader 11 is disabled" in bot.send(SAMPLE_REPORT)["text"]
    bot.send("/enable_loader 11", user_id=ADMIN_ID)
    bot.send(f"/disable_user {USER_ID}", user_id=ADMIN_ID)
    assert "not authorised to submit" in bot.send(SAMPLE_REPORT)["text"]
    assert "cannot be disabled" in bot.send(f"/disable_user {ADMIN_ID}", user_id=ADMIN_ID)["text"]


def test_start_help_and_lists(bot, container):
    assert "<code>4242</code>" in bot.send("/start", user_id=4242)["text"]
    add_loader(container, 11)
    assert "Reporter · loaders: 11" in bot.send("/start")["text"]
    help_user = bot.send("/help")["text"]
    assert "/my_loaders" in help_user and "/set_sheet" not in help_user
    assert "/set_sheet" in bot.send("/help", user_id=ADMIN_ID)["text"]
    assert "Loader 11" in bot.send("/list_loaders", user_id=ADMIN_ID)["text"]
    assert "Share the sheet with" in bot.send("/loader_info 11", user_id=ADMIN_ID)["text"]
    assert f"<code>{USER_ID}</code>" in bot.send("/users", user_id=ADMIN_ID)["text"]
    assert "Unknown command" in bot.send("/nope")["text"]


def test_check_sheet_reports_detected_layout(bot, container):
    add_loader(container, 11)
    text = bot.send("/check_sheet 11 2026 11", user_id=ADMIN_ID)["text"]
    assert "'Loader 11 nov 2026' (configured)" in text
    assert "SHIFT_1_TRIPS=D" in text and "DAY_TOTAL_TRIPS=N" in text
    assert "Date rows: 30" in text
    assert "No worksheet configured" in bot.send("/check_sheet 11 2026 12", user_id=ADMIN_ID)["text"]


def test_history_report_status_and_retry(bot, container):
    from app.services.google_sheets_service import SheetsError

    ws, rows = add_loader(container, 11)
    summary = bot.send(SAMPLE_REPORT)
    container.sheets.fail_next("write", SheetsError("SHEET_ACCESS_DENIED", "Access denied"))
    bot.confirm(summary)
    assert "FAILED" in bot.send("/history", user_id=ADMIN_ID)["text"]
    assert "NOT updated — Access denied" in bot.send("/report 1")["text"]
    bot.send("/retry_report 1", user_id=ADMIN_ID)
    assert "Retrying Loader 11" in bot.telegram.to(ADMIN_ID)[-1]["text"]
    assert ws.value(f"D{rows[REPORT_DATE]}") == 21
    assert "Follow-up update" in bot.telegram.to(USER_ID)[-1]["text"]
    assert "PARTIALLY_COMPLETED" in bot.send("/my_reports")["text"]  # loaders 01/05 are not configured


def test_trust_group_commands(bot, container):
    reply = bot.send("/trust_group -100777", user_id=ADMIN_ID)
    assert "is trusted" in reply["text"] and "ALLOW_GROUP_SUBMISSIONS is off" in reply["text"]
    assert "-100777" in bot.send("/list_groups", user_id=ADMIN_ID)["text"]
    assert "no longer trusted" in bot.send("/untrust_group -100777", user_id=ADMIN_ID)["text"]
    assert "Usage" in bot.send("/trust_group", user_id=ADMIN_ID)["text"]  # private chat, no id


def test_admins_bootstrapped_from_environment(container):
    with container.db.session() as s:
        admin = s.scalars(select(User).where(User.telegram_user_id == ADMIN_ID)).one()
        assert admin.role.value == "ADMIN" and admin.is_active


def test_commands_for_other_bots_are_ignored(make_container):
    from tests.conftest import Bot

    container = make_container(telegram_bot_username="LoaderReportBot")
    bot = Bot(container)
    assert bot.send("/help@SomeOtherBot", user_id=ADMIN_ID) == {}
    assert "/set_sheet" in bot.send("/help@loaderreportbot", user_id=ADMIN_ID)["text"]



# --- One spreadsheet holding tabs for several loaders --------------------------------------------

SHARED_ID = "shared-loaders-abcdefghijklmnop"
SHARED_URL = f"https://docs.google.com/spreadsheets/d/{SHARED_ID}/edit#gid=123"


def shared_spreadsheet(container):
    """Like the real file: 'Loader 11 nov 2026', 'Loader 01 nov 2026', 'Loader 05 nov 2026' (+ noise tabs)."""
    from datetime import date

    from tests.fakes import FakeWorksheet

    container.today = lambda: date(2026, 11, 9)
    container.sheets.add_spreadsheet(SHARED_ID, title="CHP loaders")
    tabs = {}
    for n in (11, 1, 5):
        ws, rows = build_month_sheet(f"Loader {n:02d} nov 2026", 2026, 11, loader_number=n)
        container.sheets.add_worksheet(SHARED_ID, ws)
        tabs[n] = (ws, rows)
    container.sheets.add_worksheet(SHARED_ID, FakeWorksheet("Sept 2026"))
    container.sheets.add_worksheet(SHARED_ID, FakeWorksheet("Loader 12"))
    return tabs


def test_set_sheet_without_numbers_registers_every_loader_tab(bot, container):
    shared_spreadsheet(container)
    text = bot.send(f"/set_sheet {SHARED_URL}", user_id=ADMIN_ID)["text"]
    assert "✅ Loader 01 registered · November tab: 'Loader 01 nov 2026' (auto-selected)" in text
    assert "✅ Loader 05 registered · November tab: 'Loader 05 nov 2026'" in text
    assert "✅ Loader 11 registered · November tab: 'Loader 11 nov 2026'" in text
    assert "✅ Loader 12 registered" in text and "No worksheet configured for November 2026" in text
    with container.db.session() as s:
        loaders = {l.loader_number: l.spreadsheet_id for l in s.scalars(select(Loader))}
    assert loaders == {1: SHARED_ID, 5: SHARED_ID, 11: SHARED_ID, 12: SHARED_ID}  # "Sept 2026" is not a loader


def test_set_sheet_with_several_numbers(bot, container):
    shared_spreadsheet(container)
    text = bot.send(f"/set_sheet 1 5 {SHARED_URL}", user_id=ADMIN_ID)["text"]
    assert "Loader 01 registered" in text and "Loader 05 registered" in text
    assert "also has tabs for Loader 11, Loader 12" in text
    with container.db.session() as s:
        assert sorted(l.loader_number for l in s.scalars(select(Loader))) == [1, 5]


def test_set_sheet_single_loader_points_out_other_loader_tabs(bot, container):
    shared_spreadsheet(container)
    text = bot.send(f"/set_sheet 11 {SHARED_URL}", user_id=ADMIN_ID)["text"]
    assert "also has tabs for Loader 01, Loader 05, Loader 12" in text
    assert "<code>/set_sheet 1 5 12 &lt;URL&gt;</code>" in text


def test_set_sheet_without_numbers_never_moves_a_loader_from_another_spreadsheet(bot, container):
    shared_spreadsheet(container)
    add_loader(container, 1)  # Loader 01 already has its own spreadsheet
    text = bot.send(f"/set_sheet {SHARED_URL}", user_id=ADMIN_ID)["text"]
    assert "Loader 01 has a tab here but already uses another spreadsheet — not changed" in text
    with container.db.session() as s:
        assert s.scalars(select(Loader).where(Loader.loader_number == 1)).one().spreadsheet_id == sheet_id(1)


def test_set_sheet_without_numbers_needs_access(bot, container):
    text = bot.send("/set_sheet https://docs.google.com/spreadsheets/d/unknown-sheet-abcdefghijk/edit", user_id=ADMIN_ID)["text"]
    assert "Could not open the spreadsheet" in text
    assert "Usage" in bot.send("/set_sheet 11", user_id=ADMIN_ID)["text"]


def test_report_updates_each_loader_tab_of_one_spreadsheet(bot, container):
    tabs = shared_spreadsheet(container)
    bot.send(f"/set_sheet {SHARED_URL}", user_id=ADMIN_ID)
    summary = bot.send(SAMPLE_REPORT, user_id=ADMIN_ID)
    for n in (1, 5, 11):
        assert f"'Loader {n:02d} nov 2026' (tab auto-selected)" in summary["text"]
    result = bot.confirm(summary, user_id=ADMIN_ID)
    assert result["text"].startswith("✅ SUCCESS")
    expected = {1: (42, 1340.84, 31.92), 5: (43, 1411.93, 32.84), 11: (21, 696.38, 33.16)}
    for n, (trips, weight, avg) in expected.items():
        ws, rows = tabs[n]
        r = rows[REPORT_DATE]
        assert (ws.value(f"D{r}"), ws.value(f"E{r}"), ws.value(f"F{r}")) == (trips, weight, avg)
    # each write went to its own tab of the same file
    assert sorted(w[1] for w in container.sheets.writes) == ["Loader 01 nov 2026", "Loader 05 nov 2026", "Loader 11 nov 2026"]
    assert {w[0] for w in container.sheets.writes} == {SHARED_ID}


def test_summary_points_to_tab_of_unregistered_loader(bot, container):
    shared_spreadsheet(container)
    bot.send(f"/set_sheet 11 {SHARED_URL}", user_id=ADMIN_ID)
    text = bot.send(SAMPLE_REPORT, user_id=ADMIN_ID)["text"]
    assert "Loader 01 is not registered, but tab 'Loader 01 nov 2026' is in the spreadsheet of Loader 11" in text
    assert "Loader 05 is not registered, but tab 'Loader 05 nov 2026'" in text
