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
    assert "updated" in reply["text"] and "Access OK" in reply["text"] and "'Loader 11 nov 2026'" in reply["text"]
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
