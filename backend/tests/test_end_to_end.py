"""Full flow: forwarded report -> summary -> Confirm -> sheet cells -> result message."""

from datetime import date

from sqlalchemy import func, select

from app.database.models import (
    AuditLog,
    LoaderReportRecord,
    OperationalReport,
    RecordStatus,
    ReportStatus,
    ShiftEntry,
)
from app.services.google_sheets_service import SheetsError
from tests.conftest import ADMIN_ID, OTHER_USER_ID, REPORT_DATE, SAMPLE_REPORT, USER_ID, add_loader, sheet_id


def _report(container, report_id=None) -> OperationalReport:
    with container.db.session() as s:
        stmt = select(OperationalReport).order_by(OperationalReport.id.desc())
        if report_id:
            stmt = stmt.where(OperationalReport.id == report_id)
        report = s.scalars(stmt).first()
        _ = report.records
        return report


def _records(container) -> dict[int, LoaderReportRecord]:
    return {r.loader_number: r for r in _report(container).records}


def setup_three_loaders(container):
    tabs = {}
    for n in (1, 5, 11):
        ws, rows = add_loader(container, n)
        tabs[n] = (ws, rows[REPORT_DATE])
    return tabs


def test_happy_path_updates_each_loader_sheet(bot, container):
    tabs = setup_three_loaders(container)
    ws11, row = tabs[11]
    ws11.set(f"C{row}", 120.5)  # HSD fuel
    ws11.set(f"G{row}", 10)  # existing 2nd shift
    ws11.set(f"H{row}", 300.5)
    ws11.set(f"I{row}", 30.05)

    summary = bot.send(SAMPLE_REPORT)
    text = summary["text"]
    assert "Date: <b>06-11-2026</b>" in text and "Shift: <b>1st Shift</b>" in text
    assert "<b>Loader 11</b>: 21 trips · 696.380 MT" in text
    assert "'Loader 11 nov 2026' · row" in text
    assert "106 trips · 3449.150 MT ✅ match" in text
    assert "PC 15 Nil" in text
    assert bot.telegram.buttons(summary) == [f"rc:{_report(container).id}", f"rx:{_report(container).id}"]
    assert bot.sheets.writes == []  # nothing written before confirmation

    result = bot.confirm(summary)
    assert result["text"].startswith("✅ SUCCESS")
    assert "Loader 01</b>: Updated successfully" in result["text"]
    assert "Updated 3 of 3 loader(s)" in result["text"]

    assert ws11.value(f"D{row}") == 21
    assert ws11.value(f"E{row}") == 696.38
    assert ws11.value(f"F{row}") == 33.16
    assert ws11.value(f"M{row}") == 996.88
    assert ws11.value(f"N{row}") == 31
    # untouched
    assert ws11.value(f"C{row}") == 120.5
    assert (ws11.value(f"G{row}"), ws11.value(f"H{row}"), ws11.value(f"I{row}")) == (10, 300.5, 30.05)
    assert ws11.value(f"J{row}") is None and ws11.value(f"D{row + 1}") is None

    ws1, row1 = tabs[1]
    assert (ws1.value(f"D{row1}"), ws1.value(f"E{row1}"), ws1.value(f"F{row1}")) == (42, 1340.84, 31.92)
    ws5, row5 = tabs[5]
    assert (ws5.value(f"D{row5}"), ws5.value(f"E{row5}"), ws5.value(f"F{row5}")) == (43, 1411.93, 32.84)

    # one write call per spreadsheet, each to its own document
    assert sorted(w[0] for w in bot.sheets.writes) == sorted(sheet_id(n) for n in (1, 5, 11))
    report = _report(container)
    assert report.status == ReportStatus.COMPLETED
    rec = _records(container)[11]
    assert rec.previous_values["shifts"]["2"] == {"trips": 10, "weight": "300.5"}
    assert rec.updated_values["cells"][f"D{row}"] == 21
    with container.db.session() as s:
        assert s.scalar(select(func.count()).select_from(ShiftEntry)) == 3
        assert s.scalar(select(func.count()).where(AuditLog.action == "sheet_row_updated")) == 3


def test_single_row_headers_also_work(bot, container):
    ws, rows = add_loader(container, 11, two_tier=False)
    summary = bot.send(SAMPLE_REPORT.replace("*LOADER - 01*", "x").replace("*LOADER - 05*", "y"))
    bot.confirm(summary)
    r = rows[REPORT_DATE]
    assert (ws.value(f"D{r}"), ws.value(f"E{r}"), ws.value(f"F{r}")) == (21, 696.38, 33.16)


def test_missing_configuration_does_not_block_other_loaders(bot, container):
    add_loader(container, 1)
    add_loader(container, 11)
    # Loader 05 mapped to a tab that does not exist; Loader 08 never configured.
    add_loader(container, 5, worksheet="Loader 05 november", create_tab=False)
    summary = bot.send(SAMPLE_REPORT + "\n*LOADER - 08*\nTrip:- 1 Qty:- 30")
    text = summary["text"]
    assert "Configured worksheet 'Loader 05 november'" in text
    assert "No spreadsheet configured for Loader 08" in text

    result = bot.confirm(summary)
    assert result["text"].startswith("⚠️ PARTIAL SUCCESS")
    assert "Loader 05</b>: NOT updated" in result["text"]
    assert "Updated 2 of 4 loader(s)" in result["text"]
    recs = _records(container)
    assert recs[5].error_code == "WORKSHEET_NOT_FOUND"
    assert recs[8].error_code == "LOADER_NOT_CONFIGURED"
    assert _report(container).status == ReportStatus.PARTIALLY_COMPLETED


def test_write_failure_on_one_spreadsheet_is_reported_honestly(bot, container):
    setup_three_loaders(container)
    summary = bot.send(SAMPLE_REPORT)
    # The first write (Loader 01) is rejected permanently; the others must still be written.
    bot.sheets.fail_next("write", SheetsError("SHEET_ACCESS_DENIED", "Access denied. Share the spreadsheet…"))
    result = bot.confirm(summary)
    assert result["text"].startswith("⚠️ PARTIAL SUCCESS")
    recs = _records(container)
    assert recs[1].processing_status == RecordStatus.FAILED
    assert recs[5].processing_status == RecordStatus.UPDATED
    assert recs[11].processing_status == RecordStatus.UPDATED


def test_transient_google_errors_are_retried_by_the_job(bot, container):
    import time

    _, _ = add_loader(container, 11)
    summary = bot.send(SAMPLE_REPORT)
    bot.sheets.fail_next("write", SheetsError("SHEETS_UNAVAILABLE", "503", retryable=True), times=2)
    bot.confirm(summary)
    # Intent was persisted before the failed write, so the record waits in APPLYING for the retry.
    record = _records(container)[11]
    assert record.processing_status == RecordStatus.APPLYING
    assert record.error_code == "TEMPORARY_ERROR"
    for _ in range(50):
        time.sleep(0.05)
        bot.worker.run_until_idle()
        if _records(container)[11].processing_status == RecordStatus.UPDATED:
            break
    assert _records(container)[11].processing_status == RecordStatus.UPDATED
    assert "Loader 11</b>: Updated successfully" in bot.telegram.last(USER_ID)["text"]


def test_unauthorized_loaders_are_blocked_individually(bot, container):
    add_loader(container, 1, users=())
    add_loader(container, 5, users=())
    ws, rows = add_loader(container, 11)  # USER_ID only has Loader 11
    summary = bot.send(SAMPLE_REPORT)
    assert summary["text"].count("not authorised for Loader") == 2
    bot.confirm(summary)
    assert {n: r.processing_status for n, r in _records(container).items()} == {
        1: RecordStatus.BLOCKED,
        5: RecordStatus.BLOCKED,
        11: RecordStatus.UPDATED,
    }
    assert [w[0] for w in bot.sheets.writes] == [sheet_id(11)]


def test_admin_may_report_for_any_loader(bot, container):
    add_loader(container, 11, users=())
    summary = bot.send(SAMPLE_REPORT.replace("*LOADER - 01*", "x").replace("*LOADER - 05*", "y"), user_id=ADMIN_ID)
    bot.confirm(summary, user_id=ADMIN_ID)
    assert _records(container)[11].processing_status == RecordStatus.UPDATED


def test_unregistered_user_is_refused(bot, container):
    add_loader(container, 11)
    reply = bot.send(SAMPLE_REPORT, user_id=777)
    assert "not authorised" in reply["text"] and "<code>777</code>" in reply["text"]
    with container.db.session() as s:
        assert s.scalar(select(func.count()).select_from(OperationalReport)) == 0


def test_nil_never_overwrites_existing_values(bot, container):
    ws, rows = add_loader(container, 11)
    r = rows[REPORT_DATE]
    ws.set(f"D{r}", 20)
    ws.set(f"E{r}", 650.0)
    nil_report = "DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- Nil Qty:- Nil"
    summary = bot.send(nil_report)
    assert "Nil / no values" in summary["text"]
    assert bot.telegram.buttons(summary) == []
    assert (ws.value(f"D{r}"), ws.value(f"E{r}")) == (20, 650.0)


def test_partial_nil_keeps_the_existing_weight(bot, container):
    ws, rows = add_loader(container, 11)
    r = rows[REPORT_DATE]
    ws.set(f"E{r}", 600.0)
    summary = bot.send("DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 21 Qty:- Nil")
    assert "will REPLACE</b> existing – trips / 600.000 MT" in summary["text"]  # the shift already had a weight
    bot.confirm(summary)
    assert (ws.value(f"D{r}"), ws.value(f"E{r}"), ws.value(f"F{r}")) == (21, 600.0, 28.57)


def test_re_forwarding_the_same_report_changes_nothing(bot, container):
    add_loader(container, 11)
    text = "DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 21 Qty:- 696.380"
    bot.confirm(bot.send(text))
    writes = len(bot.sheets.writes)
    again = bot.send(text)
    assert "Same values are already in the sheet" in again["text"]
    assert bot.telegram.buttons(again) == []
    assert len(bot.sheets.writes) == writes
    assert _report(container).status == ReportStatus.REJECTED


def test_correction_requires_confirmation_and_replaces_values(bot, container):
    ws, rows = add_loader(container, 11)
    r = rows[REPORT_DATE]
    bot.confirm(bot.send("DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 21 Qty:- 696.380"))
    summary = bot.send("DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 22 Qty:- 700")
    assert "will REPLACE</b> existing 21 trips / 696.380 MT" in summary["text"]
    assert ws.value(f"D{r}") == 21  # not yet
    result = bot.confirm(summary)
    assert "replaced 21 trips / 696.380 MT" in result["text"]
    assert (ws.value(f"D{r}"), ws.value(f"E{r}"), ws.value(f"N{r}")) == (22, 700.0, 22)
    with container.db.session() as s:
        entry = s.scalars(select(ShiftEntry)).one()
        assert (entry.trips, entry.revision) == (22, 2)


def test_corrections_rejected_by_policy(bot, make_container):
    from tests.conftest import Bot

    container = make_container(correction_policy="reject")
    bot = Bot(container)
    ws, rows = add_loader(container, 11)
    ws.set(f"D{rows[REPORT_DATE]}", 5)
    summary = bot.send("DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 21 Qty:- 696.380")
    assert "corrections are disabled" in summary["text"]
    assert bot.telegram.buttons(summary) == []
    assert _records(container)[11].error_code == "DUPLICATE_REJECTED"


def test_corrections_with_admin_approval(make_container):
    from tests.conftest import Bot

    container = make_container(correction_policy="admin_approval")
    bot = Bot(container)
    ws, rows = add_loader(container, 11)
    r = rows[REPORT_DATE]
    ws.set(f"D{r}", 20)
    ws.set(f"E{r}", 650.0)
    summary = bot.send("DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 21 Qty:- 696.380")
    assert "needs administrator approval" in summary["text"]
    bot.confirm(summary)
    assert _records(container)[11].processing_status == RecordStatus.AWAITING_APPROVAL
    assert _report(container).status == ReportStatus.AWAITING_APPROVAL
    assert ws.value(f"D{r}") == 20

    admin_msg = bot.telegram.to(ADMIN_ID)[-1]
    approve = next(d for d in bot.telegram.buttons(admin_msg) if d.startswith("ca:"))
    bot.press(approve, user_id=OTHER_USER_ID, message={"message_id": admin_msg["message_id"], "chat_id": OTHER_USER_ID})
    assert ws.value(f"D{r}") == 20  # a non-admin cannot approve
    bot.press(approve, user_id=ADMIN_ID, message={"message_id": admin_msg["message_id"], "chat_id": ADMIN_ID})
    assert ws.value(f"D{r}") == 21
    assert _report(container).status == ReportStatus.COMPLETED
    assert _records(container)[11].approved_by == ADMIN_ID


def test_cancel_writes_nothing(bot, container):
    add_loader(container, 11)
    summary = bot.send(SAMPLE_REPORT)
    cancel = next(d for d in bot.telegram.buttons(summary) if d.startswith("rx:"))
    bot.press(cancel, message=summary)
    assert bot.sheets.writes == []
    assert _report(container).status == ReportStatus.CANCELLED
    assert "Cancelled by" in bot.telegram.edits[-1]["text"]


def test_only_the_sender_or_an_admin_can_confirm(bot, container):
    add_loader(container, 11, users=(USER_ID, OTHER_USER_ID))
    summary = bot.send(SAMPLE_REPORT)
    bot.confirm(summary, user_id=OTHER_USER_ID)
    assert bot.sheets.writes == []
    assert bot.telegram.answers[-1]["show_alert"] is True
    bot.confirm(summary, user_id=ADMIN_ID)
    assert len(bot.sheets.writes) == 1


def test_double_confirm_applies_once(bot, container):
    add_loader(container, 11)
    summary = bot.send(SAMPLE_REPORT)
    bot.confirm(summary)
    bot.confirm(summary)
    assert len(bot.sheets.writes) == 1
    assert "Already handled" in bot.telegram.answers[-1]["text"]


def test_sheet_changed_after_summary_is_a_conflict(bot, container):
    ws, rows = add_loader(container, 11)
    summary = bot.send(SAMPLE_REPORT)
    ws.set(f"D{rows[REPORT_DATE]}", 99)  # someone typed into the sheet meanwhile
    result = bot.confirm(summary)
    assert "changed since the summary" in result["text"]
    assert ws.value(f"D{rows[REPORT_DATE]}") == 99
    assert _records(container)[11].processing_status == RecordStatus.CONFLICT


def test_auto_detected_worksheet_is_shown_before_confirmation(bot, container):
    add_loader(container, 11, worksheet=None, create_tab=False)
    from tests.fakes import build_month_sheet

    ws, rows = build_month_sheet("Loader 11 nov 2026", 2026, 11)
    container.sheets.add_worksheet(sheet_id(11), ws)
    october, _ = build_month_sheet("Loader 11 oct 2026", 2026, 10)
    container.sheets.add_worksheet(sheet_id(11), october)
    summary = bot.send(SAMPLE_REPORT)
    assert "'Loader 11 nov 2026' (tab auto-selected)" in summary["text"]
    bot.confirm(summary)
    assert ws.value(f"D{rows[REPORT_DATE]}") == 21
    assert not october.cells.get((rows[REPORT_DATE], 4))


def test_missing_date_row_is_reported(bot, container):
    ws, rows = add_loader(container, 11)
    ws.cells.pop((rows[REPORT_DATE], 2))
    summary = bot.send(SAMPLE_REPORT)
    assert "No row for 06-11-2026" in summary["text"]
    assert bot.telegram.buttons(summary) == []


def test_parse_errors_are_explained(bot, container):
    reply = bot.send("LOADER - 11 Trip:- 21 Qty:- 5", user_id=ADMIN_ID)
    assert "Could not read this as a loader report" in reply["text"]
    assert "DATE not found" in reply["text"]


def test_unsupported_message_type(bot, container):
    payload = {
        "update_id": 99,
        "message": {"message_id": 1, "date": 0, "chat": {"id": USER_ID, "type": "private"},
                    "from": {"id": USER_ID, "is_bot": False, "first_name": "U"}, "photo": [{"file_id": "x"}]},
    }
    bot._deliver(payload)
    assert "not supported" in bot.telegram.last()["text"]


def test_group_reports_need_a_trusted_group(make_container):
    from tests.conftest import Bot

    container = make_container(allow_group_submissions=True)
    bot = Bot(container)
    add_loader(container, 11)
    group = -100123
    assert bot.send(SAMPLE_REPORT, chat_id=group, chat_type="supergroup") == {}
    bot.send("/trust_group", user_id=ADMIN_ID, chat_id=group, chat_type="supergroup")
    summary = bot.send(SAMPLE_REPORT, chat_id=group, chat_type="supergroup")
    assert "Report received" in summary["text"]


def test_expired_confirmation_is_not_applied(bot, container):
    from datetime import timedelta

    from app.worker.runner import expire_stale_confirmations

    add_loader(container, 11)
    summary = bot.send(SAMPLE_REPORT)
    with container.db.session() as s:
        report = s.scalars(select(OperationalReport)).one()
        report.created_at = report.created_at - timedelta(days=1)
    assert expire_stale_confirmations(container) == 1
    bot.confirm(summary)
    assert bot.sheets.writes == []
    assert _report(container).status == ReportStatus.EXPIRED


def test_report_date_drives_monthly_tab_selection(bot, container):
    ws_nov, rows_nov = add_loader(container, 11)
    from tests.fakes import build_month_sheet
    from app.database.models import Loader, LoaderWorksheet

    ws_oct, rows_oct = build_month_sheet("Loader 11 oct 2026", 2026, 10)
    container.sheets.add_worksheet(sheet_id(11), ws_oct)
    with container.db.session() as s:
        loader = s.scalars(select(Loader)).one()
        s.add(LoaderWorksheet(loader_id=loader.id, year=2026, month=10, worksheet_name="Loader 11 oct 2026"))
    bot.confirm(bot.send(SAMPLE_REPORT.replace("06/11/2026", "31/10/2026")))
    assert ws_oct.value(f"D{rows_oct[date(2026, 10, 31)]}") == 21
    assert all(c[1] != 4 for c in ws_nov.cells if c[0] >= 4)


REPORT_0811_SHIFT3 = """*LOADER REPORT - CHP*
*DATE*:- 08/11/2026
*SHIFT*:- 3rd Shift 

*LOADER - 01*
Trip:- 30Qty:- 935.450

*LOADER - 05*
Trip:- 31 Qty:- 963.370

*LOADER - 11*
Trip:- 30 Qty:- 929.380


*PC - 15*
Trip:- Nil Qty:- Nil

*PC - 21*
Trip:- Nil Qty:- Nil

*Total Trip*:- 91
*Total Qty*:- 2828.200"""


def test_glued_qty_label_still_writes_the_weight(bot, container):
    """Regression: 'Trip:- 30Qty:- 935.450' used to write only the trips for Loader 01."""
    tabs = {n: add_loader(container, n) for n in (1, 5, 11)}
    summary = bot.send(REPORT_0811_SHIFT3)
    assert "<b>Loader 01</b>: 30 trips · 935.450 MT" in summary["text"]
    assert "91 trips · 2828.200 MT ✅ match" in summary["text"]
    bot.confirm(summary)
    day = date(2026, 11, 8)
    for n, (trips, weight) in {1: (30, 935.45), 5: (31, 963.37), 11: (30, 929.38)}.items():
        ws, rows = tabs[n]
        assert (ws.value(f"J{rows[day]}"), ws.value(f"K{rows[day]}")) == (trips, weight)


def test_unreadable_value_blocks_only_that_loader(bot, container):
    tabs = {n: add_loader(container, n) for n in (1, 11)}
    text = REPORT_0811_SHIFT3.replace("Trip:- 30Qty:- 935.450", "Trip:- 30 Qty:- MT 935").replace("*LOADER - 05*", "x")
    summary = bot.send(text)
    assert "Qty: could not read 'MT 935'" in summary["text"]
    bot.confirm(summary)
    day = date(2026, 11, 8)
    ws1, rows1 = tabs[1]
    assert ws1.value(f"J{rows1[day]}") is None  # nothing half-written for Loader 01
    ws11, rows11 = tabs[11]
    assert ws11.value(f"K{rows11[day]}") == 929.38


def test_loader_warning_is_shown_under_that_loader(bot, container):
    add_loader(container, 11)
    text = bot.send("DATE:- 06/11/2026\nSHIFT:- 1st Shift\nLOADER - 11\nTrip:- 21 Qty:- Nil")["text"]
    lines = text.split("\n")
    i = next(k for k, line in enumerate(lines) if "<b>Loader 11</b>" in line)
    assert "Qty is Nil/missing" in lines[i + 2]
