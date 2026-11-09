"""Telegram slash commands (user and administrator)."""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from app.bot.confirmation_handler import approve_correction, reject_correction
from app.bot.formatting import fmt_date, h, loader_label, render_report_brief, render_result
from app.container import AppContainer
from app.database.models import RecordStatus, ReportStatus, TrustedChat, User, UserRole
from app.database.repositories import (
    LoaderRepository,
    ReportRepository,
    TrustedChatRepository,
    UserRepository,
    audit,
)
from app.schemas.telegram import TgMessage
from app.services.google_sheets_service import SheetsError
from app.services.loader_service import ConfigurationError, LoaderService, parse_loader_number
from app.services.sheet_layout import Col, SheetLayoutError, column_letter, date_rows_summary, detect_layout
from app.services.worksheet_service import MONTH_NAMES, WorksheetResolutionError, parse_month, resolve_worksheet
from app.worker.job_queue import JobType, enqueue

log = logging.getLogger(__name__)


class UsageError(ValueError):
    pass


@dataclass
class CommandContext:
    container: AppContainer
    message: TgMessage
    user: User | None
    is_admin: bool
    args: list[str]
    update_id: int

    @property
    def actor_id(self) -> int:
        return self.message.from_user.id if self.message.from_user else 0


@dataclass(frozen=True)
class Command:
    handler: Callable[[CommandContext], str]
    usage: str
    description: str
    admin_only: bool = False
    requires_user: bool = True


COMMANDS: dict[str, Command] = {}


def command(name: str, usage: str, description: str, *, admin_only: bool = False, requires_user: bool = True):
    def register(func: Callable[[CommandContext], str]) -> Callable[[CommandContext], str]:
        COMMANDS[name] = Command(func, usage, description, admin_only, requires_user)
        return func

    return register


_COMMAND_RE = re.compile(r"^/(?P<name>[A-Za-z0-9_]+)(?:@(?P<bot>[A-Za-z0-9_]+))?(?:\s+(?P<rest>.*))?$", re.S)
_SMART_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'"})


def split_args(text: str) -> list[str]:
    try:
        return shlex.split(text.translate(_SMART_QUOTES))
    except ValueError as exc:
        raise UsageError(f"Could not read the arguments ({exc}). Check the quotes.") from exc


def handle_command(container: AppContainer, update_id: int, message: TgMessage) -> None:
    text = (message.content or "").strip()
    match = _COMMAND_RE.match(text)
    if not match:
        return
    addressed_to = match.group("bot")
    own_username = container.settings.telegram_bot_username.lstrip("@").lower()
    if addressed_to and own_username and addressed_to.lower() != own_username:
        return  # "/help@SomeOtherBot" in a group
    name = match.group("name").lower()
    cmd = COMMANDS.get(name)
    reply_to = message.message_id
    if cmd is None:
        if message.chat.is_private:
            container.telegram.send_message(message.chat.id, "Unknown command. Send /help.", reply_to_message_id=reply_to)
        return

    with container.db.session() as session:
        user = container.auth.resolve_user(session, message.from_user)
        is_admin = container.auth.is_admin(user)
        if cmd.admin_only and not is_admin:
            audit(session, "admin_command_denied", actor=message.from_user.id if message.from_user else None, command=name)

    if cmd.admin_only and not is_admin:
        reply = "⛔ This command requires administrator rights."
    elif cmd.requires_user and user is None:
        reply = "⛔ You are not registered. Send /start to see your Telegram ID and ask an administrator for access."
    else:
        try:
            ctx = CommandContext(container, message, user, is_admin, split_args(match.group("rest") or ""), update_id)
            reply = cmd.handler(ctx)
        except UsageError as exc:
            reply = f"⚠️ {h(exc)}\nUsage: <code>{h(cmd.usage)}</code>"
        except ConfigurationError as exc:
            reply = f"⚠️ {h(exc)}"
    container.telegram.send_message(message.chat.id, reply, reply_to_message_id=reply_to)


# --- Argument helpers ---------------------------------------------------------------------------


def _loader_arg(args: list[str], index: int = 0) -> int:
    if len(args) <= index:
        raise UsageError("Loader number is missing")
    return parse_loader_number(args[index])


def _int_arg(args: list[str], index: int, what: str) -> int:
    if len(args) <= index:
        raise UsageError(f"{what} is missing")
    token = args[index].lstrip("#")
    if not re.fullmatch(r"-?\d+", token):
        raise UsageError(f"{what} must be a number, got '{args[index]}'")
    return int(token)


def _year_month(args: list[str], start: int) -> tuple[int, int]:
    year = _int_arg(args, start, "Year")
    if len(args) <= start + 1:
        raise UsageError("Month is missing")
    month = parse_month(args[start + 1])
    if month is None:
        raise UsageError(f"Invalid month '{args[start + 1]}' (use 1-12 or a month name)")
    return year, month


# --- Everyone -------------------------------------------------------------------------------------


@command("start", "/start", "Show your Telegram ID and access", requires_user=False)
def cmd_start(ctx: CommandContext) -> str:
    tg_id = ctx.actor_id
    if ctx.user is None:
        return (
            "👋 Hello! This bot updates the loader spreadsheets from forwarded WhatsApp reports.\n\n"
            f"Your Telegram user ID is <code>{tg_id}</code>.\n"
            "You are not registered yet — ask an administrator to run:\n"
            f"<code>/grant_loader {tg_id} &lt;loader&gt;</code>"
        )
    with ctx.container.db.session() as session:
        user = UserRepository(session).get_by_telegram_id(tg_id)
        loaders = ", ".join(f"{l.loader_number:02d}" for l in user.loaders) or "none"
    role = "Administrator (all loaders)" if ctx.is_admin else f"Reporter · loaders: {loaders}"
    return (
        f"👋 Welcome, {h(ctx.user.display_name)}.\nID <code>{tg_id}</code> · {role}\n\n"
        "Forward a WhatsApp loader report here; you will get a summary to confirm before any sheet is changed.\n"
        "Send /help for commands."
    )


@command("help", "/help", "List commands", requires_user=False)
def cmd_help(ctx: CommandContext) -> str:
    lines = ["<b>Commands</b>"]
    for name, cmd in COMMANDS.items():
        if not cmd.admin_only:
            lines.append(f"<code>{h(cmd.usage)}</code> — {h(cmd.description)}")
    if ctx.is_admin:
        lines += ["", "<b>Administrator</b>"]
        for name, cmd in COMMANDS.items():
            if cmd.admin_only:
                lines.append(f"<code>{h(cmd.usage)}</code> — {h(cmd.description)}")
    return "\n".join(lines)


@command("my_loaders", "/my_loaders", "Loaders you may report for")
def cmd_my_loaders(ctx: CommandContext) -> str:
    with ctx.container.db.session() as session:
        if ctx.is_admin:
            loaders = LoaderRepository(session).list_all()
            header = "As an administrator you may report for every active loader:"
        else:
            loaders = UserRepository(session).get_by_telegram_id(ctx.actor_id).loaders
            header = "Your loaders:"
        if not loaders:
            return "No loaders assigned yet. Ask an administrator."
        lines = [header] + [f"• {l.label}" + ("" if l.is_active else " (disabled)") for l in loaders]
    return "\n".join(lines)


@command("my_reports", "/my_reports", "Your recent reports and their results")
def cmd_my_reports(ctx: CommandContext) -> str:
    with ctx.container.db.session() as session:
        reports = ReportRepository(session).recent(limit=10, telegram_user_id=ctx.actor_id)
        if not reports:
            return "You have not submitted any reports yet."
        return "<b>Your recent reports</b>\n" + "\n".join(render_report_brief(r) for r in reports) + "\n\nDetails: /report &lt;id&gt;"


@command("report", "/report <id>", "Processing result of a report")
def cmd_report(ctx: CommandContext) -> str:
    report_id = _int_arg(ctx.args, 0, "Report id")
    with ctx.container.db.session() as session:
        report = ReportRepository(session).get(report_id)
        if report is None or (report.telegram_user_id != ctx.actor_id and not ctx.is_admin):
            return f"Report #{report_id} not found."
        return render_result(report)


# --- Loaders & worksheets (admin) ---------------------------------------------------------------


@command("set_sheet", "/set_sheet <loader> <spreadsheet URL>", "Register or change a loader's spreadsheet", admin_only=True)
def cmd_set_sheet(ctx: CommandContext) -> str:
    number = _loader_arg(ctx.args)
    if len(ctx.args) < 2:
        raise UsageError("Spreadsheet URL is missing")
    with ctx.container.db.session() as session:
        result = LoaderService(session).set_sheet(number, ctx.args[1], actor=ctx.actor_id)
        spreadsheet_id = result.loader.spreadsheet_id
        mappings = len(LoaderRepository(session).list_worksheets(result.loader.id))
    lines = [
        f"✅ {loader_label(number)} {'registered' if result.created else 'updated'}.",
        f"Spreadsheet ID: <code>{h(spreadsheet_id)}</code>",
    ]
    if result.previous_url and result.previous_url != ctx.args[1]:
        lines.append(f"Previous URL: {h(result.previous_url)}")
        if mappings:
            lines.append(f"{mappings} worksheet mapping(s) kept — review with /list_worksheets {number}.")
    lines.append(_access_check(ctx.container, spreadsheet_id))
    return "\n".join(lines)


def _access_check(container: AppContainer, spreadsheet_id: str) -> str:
    try:
        info = container.sheets.get_spreadsheet_info(spreadsheet_id)
    except SheetsError as exc:
        return f"⚠️ Saved, but the spreadsheet is not accessible yet: {h(exc.message)}"
    tabs = ", ".join(f"'{h(t)}'" for t in info.worksheet_titles[:15])
    more = " …" if len(info.worksheet_titles) > 15 else ""
    return f"🔗 Access OK: <b>{h(info.title)}</b>\nTabs: {tabs}{more}"


@command("list_loaders", "/list_loaders", "All loaders and their configuration", admin_only=True)
def cmd_list_loaders(ctx: CommandContext) -> str:
    with ctx.container.db.session() as session:
        loaders = LoaderRepository(session).list_all()
        if not loaders:
            return "No loaders configured. Use /set_sheet &lt;loader&gt; &lt;URL&gt;."
        lines = ["<b>Loaders</b>"]
        for l in loaders:
            state = "✅ active" if l.is_active else "⛔ disabled"
            lines.append(
                f"• <b>{l.label}</b> · {state} · sheet <code>{h(l.spreadsheet_id[:12])}…</code> · "
                f"{len(l.worksheets)} tab mapping(s) · {len(l.users)} user(s)"
            )
    return "\n".join(lines)


@command("loader_info", "/loader_info <loader>", "Details of one loader", admin_only=True)
def cmd_loader_info(ctx: CommandContext) -> str:
    number = _loader_arg(ctx.args)
    with ctx.container.db.session() as session:
        loader = LoaderService(session).require(number)
        mappings = LoaderRepository(session).list_worksheets(loader.id)
        lines = [
            f"<b>{loader.label}</b> — {'✅ active' if loader.is_active else '⛔ disabled'}",
            f"URL: {h(loader.spreadsheet_url)}",
            f"Spreadsheet ID: <code>{h(loader.spreadsheet_id)}</code>",
            "Worksheets: "
            + (", ".join(f"{m.year}-{m.month:02d} → '{h(m.worksheet_name)}'" for m in mappings[:6]) or "none configured"),
            "Users: " + (", ".join(f"<code>{u.telegram_user_id}</code>" for u in loader.users) or "none (admins only)"),
        ]
    if ctx.container.sheets.service_account_email:
        lines.append(f"Share the sheet with: <code>{h(ctx.container.sheets.service_account_email)}</code>")
    return "\n".join(lines)


def _set_active(ctx: CommandContext, active: bool) -> str:
    number = _loader_arg(ctx.args)
    with ctx.container.db.session() as session:
        LoaderService(session).set_active(number, active, actor=ctx.actor_id)
    return f"{'✅' if active else '⛔'} {loader_label(number)} {'enabled' if active else 'disabled'}."


@command("disable_loader", "/disable_loader <loader>", "Stop accepting updates for a loader", admin_only=True)
def cmd_disable_loader(ctx: CommandContext) -> str:
    return _set_active(ctx, False)


@command("enable_loader", "/enable_loader <loader>", "Accept updates for a loader again", admin_only=True)
def cmd_enable_loader(ctx: CommandContext) -> str:
    return _set_active(ctx, True)


@command(
    "set_worksheet",
    '/set_worksheet <loader> <year> <month> "<tab name>"',
    "Map a month to a worksheet tab",
    admin_only=True,
)
def cmd_set_worksheet(ctx: CommandContext) -> str:
    number = _loader_arg(ctx.args)
    year, month = _year_month(ctx.args, 1)
    if len(ctx.args) < 4:
        raise UsageError("Worksheet (tab) name is missing")
    name = " ".join(ctx.args[3:])
    with ctx.container.db.session() as session:
        mapping, previous = LoaderService(session).set_worksheet(number, year, month, name, actor=ctx.actor_id)
        spreadsheet_id = mapping.loader.spreadsheet_id
    lines = [f"✅ {loader_label(number)} · {MONTH_NAMES[month]} {year} → '{h(name)}'"]
    if previous and previous != name:
        lines.append(f"(was '{h(previous)}')")
    try:
        titles = ctx.container.sheets.get_spreadsheet_info(spreadsheet_id).worksheet_titles
        if name not in titles:
            lines.append(
                "⚠️ No tab with exactly this name exists yet. Updates for this month will fail until it does.\n"
                "Existing tabs: " + ", ".join(f"'{h(t)}'" for t in titles[:20])
            )
    except SheetsError as exc:
        lines.append(f"⚠️ Could not verify the tab: {h(exc.message)}")
    return "\n".join(lines)


@command("remove_worksheet", "/remove_worksheet <loader> <year> <month>", "Remove a month→tab mapping", admin_only=True)
def cmd_remove_worksheet(ctx: CommandContext) -> str:
    number = _loader_arg(ctx.args)
    year, month = _year_month(ctx.args, 1)
    with ctx.container.db.session() as session:
        removed = LoaderService(session).remove_worksheet(number, year, month, actor=ctx.actor_id)
    if not removed:
        return f"No mapping for {loader_label(number)} {MONTH_NAMES[month]} {year}."
    return f"🗑 Removed mapping for {loader_label(number)} {MONTH_NAMES[month]} {year}."


@command("list_worksheets", "/list_worksheets <loader>", "Month→tab mappings of a loader", admin_only=True)
def cmd_list_worksheets(ctx: CommandContext) -> str:
    number = _loader_arg(ctx.args)
    with ctx.container.db.session() as session:
        loader = LoaderService(session).require(number)
        mappings = LoaderRepository(session).list_worksheets(loader.id)
        if not mappings:
            auto = "on" if ctx.container.settings.worksheet_auto_detect else "off"
            return f"No worksheet mappings for {loader.label} (auto-detection is {auto})."
        lines = [f"<b>{loader.label} worksheets</b>"] + [
            f"• {MONTH_NAMES[m.month]} {m.year} → '{h(m.worksheet_name)}'" + ("" if m.is_active else " (inactive)")
            for m in mappings
        ]
    return "\n".join(lines)


@command("check_sheet", "/check_sheet <loader> [year month]", "Check worksheet, headers and date rows", admin_only=True)
def cmd_check_sheet(ctx: CommandContext) -> str:
    number = _loader_arg(ctx.args)
    today = ctx.container.today()
    year, month = _year_month(ctx.args, 1) if len(ctx.args) >= 3 else (today.year, today.month)
    settings = ctx.container.settings
    with ctx.container.db.session() as session:
        service = LoaderService(session)
        loader = service.require(number)
        spreadsheet_id = loader.spreadsheet_id
        configured = service.configured_worksheet(loader, year, month)
    sheets = ctx.container.sheets
    lines = [f"🔎 <b>{loader_label(number)}</b> · {MONTH_NAMES[month]} {year}"]
    try:
        info = sheets.get_spreadsheet_info(spreadsheet_id)
        lines.append(f"Spreadsheet: <b>{h(info.title)}</b>")
        resolution = resolve_worksheet(
            loader_number=number,
            year=year,
            month=month,
            configured_name=configured,
            available_titles=info.worksheet_titles,
            auto_detect=settings.worksheet_auto_detect,
        )
        how = "auto-detected" if resolution.auto_detected else "configured"
        lines.append(f"Worksheet: '{h(resolution.worksheet_name)}' ({how})")
        grid = sheets.read_grid(
            spreadsheet_id, resolution.worksheet_name, last_row=settings.sheet_read_max_rows, last_col=settings.sheet_read_last_column
        )
        layout = detect_layout(
            grid,
            settings.sheet_column_map,
            detection=settings.sheet_header_detection,
            strict=settings.sheet_header_strict,
            scan_rows=settings.sheet_header_scan_rows,
        )
    except (SheetsError, WorksheetResolutionError, SheetLayoutError) as exc:
        lines.append(f"❌ {h(exc.message)}")
        return "\n".join(lines)
    lines.append(f"Header row(s): {', '.join(map(str, layout.header_rows))} · data from row {layout.data_start_row}")
    lines.append(
        "Columns: " + ", ".join(f"{c.value}={column_letter(layout.columns[c])}" for c in Col if c in layout.columns)
    )
    if layout.deviations:
        lines.append("⚠️ Differs from the configured mapping: " + "; ".join(layout.deviations))
    rows = date_rows_summary(grid, layout)
    if rows:
        lines.append(
            f"Date rows: {len(rows)} (row {rows[0][0]} = {fmt_date(rows[0][1])} … row {rows[-1][0]} = {fmt_date(rows[-1][1])})"
        )
        outside = [d for _, d in rows if (d.year, d.month) != (year, month)]
        if outside:
            lines.append(f"⚠️ {len(outside)} date row(s) are outside {MONTH_NAMES[month]} {year}")
    else:
        lines.append("⚠️ No date rows found below the header")
    return "\n".join(lines)


# --- Users & access (admin) -----------------------------------------------------------------


def _loader_numbers(args: list[str], start: int) -> list[int]:
    if len(args) <= start:
        raise UsageError("Loader number is missing")
    return [parse_loader_number(a) for a in args[start:]]


@command("grant_loader", "/grant_loader <user_id> <loader> [loader…]", "Allow a user to report for loaders", admin_only=True)
def cmd_grant_loader(ctx: CommandContext) -> str:
    target = _int_arg(ctx.args, 0, "User id")
    numbers = _loader_numbers(ctx.args, 1)
    with ctx.container.db.session() as session:
        users = UserRepository(session)
        service = LoaderService(session)
        loaders = [service.require(n) for n in numbers]
        user, created = users.get_or_create(target)
        if not user.is_active:
            user.is_active = True
        granted = [l.label for l in loaders if users.grant_loader(user, l, ctx.actor_id)]
        audit(session, "loader_granted", actor=ctx.actor_id, entity_type="user", entity_id=target, loaders=numbers)
    new = " (new user)" if created else ""
    if not granted:
        return f"User <code>{target}</code> already had access to {', '.join(loader_label(n) for n in numbers)}."
    return f"✅ User <code>{target}</code>{new} may now report for: {', '.join(granted)}."


@command("revoke_loader", "/revoke_loader <user_id> <loader> [loader…]", "Remove a user's access to loaders", admin_only=True)
def cmd_revoke_loader(ctx: CommandContext) -> str:
    target = _int_arg(ctx.args, 0, "User id")
    numbers = _loader_numbers(ctx.args, 1)
    with ctx.container.db.session() as session:
        users = UserRepository(session)
        user = users.get_by_telegram_id(target)
        if user is None:
            return f"User <code>{target}</code> is not registered."
        service = LoaderService(session)
        revoked = [service.require(n).label for n in numbers if users.revoke_loader(user, service.require(n))]
        audit(session, "loader_revoked", actor=ctx.actor_id, entity_type="user", entity_id=target, loaders=numbers)
    if not revoked:
        return f"User <code>{target}</code> had no access to those loaders."
    return f"🗑 Revoked {', '.join(revoked)} from user <code>{target}</code>."


@command("users", "/users", "Registered users and their loaders", admin_only=True)
def cmd_users(ctx: CommandContext) -> str:
    with ctx.container.db.session() as session:
        users = UserRepository(session).list_all()
        lines = ["<b>Users</b>"]
        for u in users:
            loaders = "all" if u.role == UserRole.ADMIN else (", ".join(f"{l.loader_number:02d}" for l in u.loaders) or "none")
            state = "" if u.is_active else " · ⛔ disabled"
            lines.append(f"• <code>{u.telegram_user_id}</code> {h(u.display_name)} · {u.role.value} · loaders: {loaders}{state}")
    return "\n".join(lines)


def _set_user_active(ctx: CommandContext, active: bool) -> str:
    target = _int_arg(ctx.args, 0, "User id")
    if not active and target in ctx.container.settings.admin_ids:
        return "⛔ Administrators from ADMIN_TELEGRAM_IDS cannot be disabled from Telegram."
    with ctx.container.db.session() as session:
        user = UserRepository(session).get_by_telegram_id(target)
        if user is None:
            return f"User <code>{target}</code> is not registered."
        user.is_active = active
        audit(session, "user_enabled" if active else "user_disabled", actor=ctx.actor_id, entity_type="user", entity_id=target)
    return f"{'✅ Enabled' if active else '⛔ Disabled'} user <code>{target}</code>."


@command("disable_user", "/disable_user <user_id>", "Block a user completely", admin_only=True)
def cmd_disable_user(ctx: CommandContext) -> str:
    return _set_user_active(ctx, False)


@command("enable_user", "/enable_user <user_id>", "Unblock a user", admin_only=True)
def cmd_enable_user(ctx: CommandContext) -> str:
    return _set_user_active(ctx, True)


# --- Groups (admin) -------------------------------------------------------------------------------


def _chat_arg(ctx: CommandContext) -> tuple[int, str | None]:
    if ctx.args:
        return _int_arg(ctx.args, 0, "Chat id"), None
    if ctx.message.chat.is_group:
        return ctx.message.chat.id, ctx.message.chat.title
    raise UsageError("Run this inside the group, or pass the group's chat id")


@command("trust_group", "/trust_group [chat_id]", "Accept reports from a group chat", admin_only=True)
def cmd_trust_group(ctx: CommandContext) -> str:
    chat_id, title = _chat_arg(ctx)
    with ctx.container.db.session() as session:
        repo = TrustedChatRepository(session)
        chat = repo.get(chat_id)
        if chat is None:
            session.add(TrustedChat(chat_id=chat_id, title=title, is_active=True, added_by=ctx.actor_id))
        else:
            chat.is_active = True
            chat.title = title or chat.title
        audit(session, "group_trusted", actor=ctx.actor_id, entity_type="chat", entity_id=chat_id)
    note = "" if ctx.container.settings.allow_group_submissions else (
        "\n⚠️ ALLOW_GROUP_SUBMISSIONS is off, so group reports are still ignored until it is enabled."
    )
    return f"✅ Group <code>{chat_id}</code> is trusted. Members still need loader access.{note}"


@command("untrust_group", "/untrust_group [chat_id]", "Stop accepting reports from a group", admin_only=True)
def cmd_untrust_group(ctx: CommandContext) -> str:
    chat_id, _ = _chat_arg(ctx)
    with ctx.container.db.session() as session:
        chat = TrustedChatRepository(session).get(chat_id)
        if chat is None or not chat.is_active:
            return f"Group <code>{chat_id}</code> was not trusted."
        chat.is_active = False
        audit(session, "group_untrusted", actor=ctx.actor_id, entity_type="chat", entity_id=chat_id)
    return f"🗑 Group <code>{chat_id}</code> is no longer trusted."


@command("list_groups", "/list_groups", "Trusted group chats", admin_only=True)
def cmd_list_groups(ctx: CommandContext) -> str:
    with ctx.container.db.session() as session:
        chats = [c for c in TrustedChatRepository(session).list_all() if c.is_active]
        if not chats:
            return "No trusted groups."
        return "<b>Trusted groups</b>\n" + "\n".join(f"• <code>{c.chat_id}</code> {h(c.title or '')}" for c in chats)


# --- History, retries, corrections (admin) --------------------------------------------------------


@command("history", "/history [count]", "Recent reports from everyone", admin_only=True)
def cmd_history(ctx: CommandContext) -> str:
    limit = min(max(_int_arg(ctx.args, 0, "Count"), 1), 50) if ctx.args else 15
    with ctx.container.db.session() as session:
        reports = ReportRepository(session).recent(limit=limit)
        if not reports:
            return "No reports yet."
        return "<b>Recent reports</b>\n" + "\n".join(render_report_brief(r) for r in reports)


@command("retry_report", "/retry_report <id>", "Retry loaders that failed to update", admin_only=True)
def cmd_retry_report(ctx: CommandContext) -> str:
    report_id = _int_arg(ctx.args, 0, "Report id")
    with ctx.container.db.session() as session:
        repo = ReportRepository(session)
        report = repo.get(report_id)
        if report is None:
            return f"Report #{report_id} not found."
        failed = [r for r in report.records if r.processing_status == RecordStatus.FAILED]
        if not failed:
            return f"Report #{report_id} has no failed loaders to retry (conflicts and blocked loaders need a new report)."
        ids = []
        for record in failed:
            if repo.transition_record(record.id, [RecordStatus.FAILED], RecordStatus.QUEUED, error_code=None, error_message=None):
                ids.append(record.id)
        report.status = ReportStatus.CONFIRMED
        report.completed_at = None
        enqueue(
            session,
            JobType.APPLY_REPORT,
            {"report_id": report_id, "record_ids": ids, "reason": "retry requested by an administrator"},
            max_attempts=ctx.container.settings.job_max_attempts,
        )
        audit(session, "report_retry", actor=ctx.actor_id, entity_type="report", entity_id=report_id, records=ids)
        labels = ", ".join(loader_label(r.loader_number) for r in failed)
    ctx.container.wake_worker()
    return f"🔁 Retrying {labels} for report #{report_id}. The result will be posted in the original chat."


@command("approve_correction", "/approve_correction <record_id>", "Approve a pending correction", admin_only=True)
def cmd_approve_correction(ctx: CommandContext) -> str:
    return h(approve_correction(ctx.container, ctx.user, _int_arg(ctx.args, 0, "Record id"))[1])


@command("reject_correction", "/reject_correction <record_id>", "Reject a pending correction", admin_only=True)
def cmd_reject_correction(ctx: CommandContext) -> str:
    return h(reject_correction(ctx.container, ctx.user, _int_arg(ctx.args, 0, "Record id"))[1])
