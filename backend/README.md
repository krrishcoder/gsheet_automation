# Loader Report Bot

Forward a WhatsApp **LOADER REPORT** to a Telegram bot; it extracts every loader's trips and
quantity, shows a summary to confirm, then writes each loader's figures into **that loader's own
Google Spreadsheet**, in the right **monthly tab**, on the row for the **report date**, in the
columns for the **shift** — and recalculates the shift average and day totals.

```
Telegram ──POST /telegram/webhook──▶ FastAPI ──(1 transaction)──▶ telegram_updates + jobs ──▶ 200 OK
                                                                         │
                         background worker (DB-backed queue, survives restarts)
                                                                         ▼
     parse ─▶ validate ─▶ preview every loader against its sheet ─▶ summary + [Confirm] [Cancel]
                                                                         │ Confirm
                                                                         ▼
     per loader: lock tab ─▶ re-read row ─▶ conflict check ─▶ save intent ─▶ write cells ─▶ record result
                                                                         ▼
                                              result message (per-loader success / failure)
```

---

## 1. Assumptions to confirm

The implementation makes these choices. Each is either configurable or easy to change — please
confirm or correct them before go-live.

| # | Assumption | Where to change |
|---|---|---|
| 1 | The sheets look like the screenshots (headers *Date, HSD Fuel (L), 1st/2nd/3rd Shift × Trips/Weight (MT)/Avg (MT), Day Total Weight/Trips*), either as one header row or a merged "1st Shift" group row above "Trips / Weight / Avg". Columns are located **by header text**, not by fixed letters. Run `/check_sheet <loader>` on each real sheet to confirm detection. | `SHEET_HEADER_*`, `SHEET_COLUMN_MAP` |
| 2 | Avg (F/I/L) and Day Total (M/N) cells: if they already contain **formulas**, the bot leaves the formulas alone and only writes Trips/Weight. If they hold plain values, the bot writes the computed values. **Confirmed 2026-11-09:** the live sheets use formulas in C, F, I, L, M and N. | automatic |
| 3 | "Total Trip"/"Total Qty" in the report cover **all equipment in the message (loaders + PC)**. A mismatch is shown as a warning and never changes loader values. | `validation_service.py` |
| 4 | Dates are **day-first** (06/11/2026 = 6 Nov). Date cells in the sheet are real dates (serial numbers) or DD-MM-YYYY text. | — |
| 5 | Shifts are 1st/2nd/3rd (also *first*, *II*, *2nd.shipt*, *Shift 3*…). Names like *A/B/C*, *General*, *Night* are rejected rather than guessed. | `report_parser.py` |
| 6 | Weight is written with 3 decimals, the average rounded half-up to 2 decimals (696.380 / 21 = 33.16). | `WEIGHT_DECIMALS`, `AVERAGE_DECIMALS` |
| 7 | A partially Nil entry (e.g. `Trip:- 21 Qty:- Nil`) writes only the reported value; the other cell keeps whatever the sheet has. A fully Nil entry writes nothing. | — |
| 8 | Administrators may submit for every loader; users only for loaders granted to them. | `authorization_service.py` |
| 9 | Re-submitting data for a date+shift that already has values is a **correction**: the summary shows *old → new* and Confirm replaces (never adds). Alternatives: reject, or require admin approval. | `CORRECTION_POLICY` |
| 10 | When no explicit month→tab mapping exists, a tab is auto-selected only if **exactly one** tab title contains the loader number, the month and the year (e.g. `Loader 11 nov 2026`, `Oct loader 08 2026`). Tabs like `Sept 2026` or `Loader 12` need `/set_worksheet`. The chosen tab is always shown before confirmation. | `WORKSHEET_AUTO_DETECT` |
| 11 | Worksheets are **never created** by the bot. A month tab without a loader number (`Nov 2026`) may hold one table per loader; each table must be titled "Loader NN …". A missing tab or date row is reported as an error. | — |
| 12 | Reports from group chats are off by default (`ALLOW_GROUP_SUBMISSIONS`); when enabled, only groups trusted with `/trust_group` count, and every sender still needs per-loader access. | `ALLOW_GROUP_SUBMISSIONS` |

---

## 2. What happens to a report

Example input (formatting may vary — extra spaces, `*bold*`, `:-`, `–` dashes, different case):

```
*LOADER REPORT - CHP*
*DATE*:- 06/11/2026
*SHIFT*:- 1st Shift
*LOADER - 01*
Trip:- 42 Qty:-1340.840
*LOADER - 11*
Trip:- 21 Qty:-696.380
*PC - 15*
Trip:- Nil Qty:- Nil
*Total Trip*:-63
*Total Qty*:-2037.220
```

1. **Parse** (`services/report_parser.py`, regular expressions, no LLM): date → `2026-11-06`,
   shift → `1`, one entry per `LOADER - NN`. `PC - NN` is kept as *other equipment* and never routed
   to a loader sheet. `Nil` = unavailable (not zero); `0` = a real zero. Unreadable numbers
   (e.g. `696,380`) are an error for that loader, never guessed.
2. **Validate** (`services/validation_service.py`): totals cross-check, quantity with 0 trips
   (blocks that loader), future date, partial values.
3. **Preview** each loader independently (`services/report_processor.py`): loader configured and
   active? user authorised for *this* loader? monthly tab found? headers recognised? date row found?
   current values of the shift? → each loader becomes READY, SKIPPED (Nil / identical values) or
   BLOCKED (with the reason). Nothing is written yet.
4. **Summary** with *Confirm Update* / *Cancel* (expires after `CONFIRMATION_TTL_MINUTES`). Only the
   sender or an admin can press them.
5. **Apply** after Confirm, per loader: lock the tab → re-read the row → if the shift's cells changed
   since the summary, stop with CONFLICT → store old values + planned values → write cells → mark
   UPDATED + audit log.
6. **Result** message listing each loader's outcome — `SUCCESS`, `PARTIAL SUCCESS` or `FAILED`.

For Loader 11 above, row *06-11-2026* of tab *Loader 11 nov 2026*:

| Cell | Written |
|---|---|
| D (1st Shift Trips) | `21` |
| E (1st Shift Weight) | `696.38` (number; the cell's `0.000 "MT"` format shows 696.380 MT) |
| F (1st Shift Avg) | `33.16` |
| M (Day Total Weight) | 1st + 2nd + 3rd shift weights **as they are in the row after the update** |
| N (Day Total Trips) | same for trips |

HSD Fuel (C), the other shifts' cells and every other row are never written. Values are written
with `valueInputOption=RAW`, so they are numbers, and cell number formats are untouched.

---

## 3. Project layout

```
backend/
├── app/
│   ├── main.py                     FastAPI app + lifespan (starts the in-process worker)
│   ├── container.py                dependency wiring (tests inject fakes here)
│   ├── api/telegram_webhook.py     POST /telegram/webhook: verify secret, persist, 200
│   ├── api/health.py               GET /health
│   ├── bot/dispatcher.py           routes an update to a handler
│   ├── bot/message_handler.py      report → parse → validate → preview → summary
│   ├── bot/confirmation_handler.py Confirm/Cancel, correction Approve/Reject buttons
│   ├── bot/commands.py             /commands
│   ├── bot/formatting.py           Telegram message texts
│   ├── services/report_parser.py   WhatsApp format parser
│   ├── services/validation_service.py
│   ├── services/report_processor.py  per-loader preview/apply state machine
│   ├── services/sheet_layout.py    header detection, date rows, cell read/write plan
│   ├── services/calculations.py    Decimal averages and day totals
│   ├── services/worksheet_service.py monthly tab selection
│   ├── services/google_sheets_service.py  Sheets API v4 client + retries
│   ├── services/telegram_service.py Bot API client + retries
│   ├── services/authorization_service.py, loader_service.py, locks.py, retry.py
│   ├── worker/job_queue.py         durable DB job queue
│   ├── worker/handlers.py          job handlers
│   ├── worker/runner.py            worker loop (thread or `python -m app.worker.runner`)
│   ├── database/models.py, database.py, repositories.py
│   ├── schemas/report.py, telegram.py, sheets.py
│   └── core/config.py, security.py, logging.py
├── scripts/set_webhook.py          register / inspect / delete the webhook
├── tests/                          225 tests, external APIs faked
├── deploy/Caddyfile                HTTPS reverse proxy (EC2)
├── Dockerfile, docker-compose.yml, .env.example, requirements*.txt
```

Deviations from the suggested structure, and why: the worker lives in `app/worker/` (it is a
separate runtime concern, and can run as its own process); sheet layout and calculations are their
own pure modules so they can be tested without Google; `container.py` wires dependencies so tests
can swap in fake Telegram/Sheets clients.

---

## 4. Setup

### 4.1 Google service account

**Current setup:** the local `backend/.env` (not in git) already contains a service account key
reused from another project; its e-mail address is in the comment above `GOOGLE_SERVICE_ACCOUNT_JSON`
and is also shown by `/loader_info`. Share every loader spreadsheet with that address as **Editor**.
That account can then reach both projects' sheets; for separation, create a dedicated account with
the steps below and replace `GOOGLE_SERVICE_ACCOUNT_JSON`.

1. In [Google Cloud Console](https://console.cloud.google.com/) create (or pick) a project.
2. *APIs & Services → Library →* enable **Google Sheets API**.
3. *IAM & Admin → Service Accounts → Create*. No roles are needed.
4. Open the account → *Keys → Add key → JSON*. Save it as `backend/secrets/google-service-account.json`
   (or paste the JSON into `GOOGLE_SERVICE_ACCOUNT_JSON`). Keep it out of git.
5. **Share every loader spreadsheet** with the service account's e-mail
   (`…@….iam.gserviceaccount.com`) as **Editor**. `/loader_info` shows the address; `/set_sheet`
   tells you if access is missing.

### 4.2 Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. `python scripts/set_webhook.py generate-secret` → put it in `TELEGRAM_WEBHOOK_SECRET`.
3. Send `/start` to your bot (once the server runs) to learn your Telegram ID → `ADMIN_TELEGRAM_IDS`.
   Every admin should open a private chat with the bot once, so it can message them.
4. For group submissions, also disable privacy mode in BotFather (`/setprivacy → Disable`), otherwise
   the bot only sees commands in groups.

### 4.3 Configuration

`backend/.env` holds the local settings (git-ignored; `.env.example` documents every option). The
app reads `.env` from the directory it is started in, i.e. `backend/`. On Render the file is not
used; set the variables in the dashboard. Required:
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `ADMIN_TELEGRAM_IDS`, and
`GOOGLE_SERVICE_ACCOUNT_FILE` or `GOOGLE_SERVICE_ACCOUNT_JSON`. The server refuses to start without them.

---

## 5. Running locally

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

```bash
uvicorn app.main:app --port 8000
```

Telegram needs a public HTTPS URL. For local testing use a tunnel, e.g.
`cloudflared tunnel --url http://localhost:8000`, set `PUBLIC_BASE_URL` to the printed URL, then:

```bash
python scripts/set_webhook.py set
```

---

## 6. Onboarding loaders (in Telegram, as an admin)

```
/set_sheet 11 https://docs.google.com/spreadsheets/d/<id>/edit
/set_worksheet 11 2026 11 "Loader 11 nov 2026"
/check_sheet 11 2026 11            ← verify tab, detected columns, date rows
/grant_loader 123456789 11         ← let a reporter submit for Loader 11
```

Repeat `/set_worksheet` every month (or rely on auto-detection when tab names follow the
*Loader NN mon YYYY* pattern). Changing a spreadsheet URL keeps the month→tab mappings.

**One spreadsheet with tabs for several loaders** (e.g. `Loader 11 nov 2026`, `Loader 01 nov 2026`,
`Loader 05 nov 2026` in one file): the link identifies the file, never a tab (`#gid=…` is ignored).
Register it once for all its loaders:

```
/set_sheet https://docs.google.com/spreadsheets/d/<id>/edit        ← every "Loader NN …" tab in the file
/set_sheet 1 5 11 https://docs.google.com/spreadsheets/d/<id>/edit ← or exactly these loaders
```

Each loader then uses only its own tab (chosen from the report's month), and each tab is locked
separately while it is written. Without loader numbers, a tab counts only if its name contains
"Loader" and exactly one loader number (`Sept 2026` or `Loader 11 & 12` are ignored), and a loader
that already uses a different spreadsheet is never moved — the reply lists it instead. If a report
names a loader that is not registered but its tab is in a spreadsheet the report already uses, the
summary says so.

**One tab with a separate table per loader** (e.g. tab `Loader Report nov 2026` holding the tables
*Loader 11 Whatsapp*, *Loader 01 Whatsapp*, *Loader 05 Whatsapp*, side by side or one under another):

* Give each table a title above its header row that contains **"Loader" and its number**
  (`Loader 05 Whatsapp`, `Loader 05 – November 2026`). Each table needs its own *Date* column.
* Name the tab for the month without a loader number (`Nov 2026`, `Loader Report nov 2026`). With no
  `/set_worksheet` mapping, that tab is used for every loader that has no tab of its own for the month,
  and only if it is the only such tab. You can also map it explicitly: `/set_worksheet 5 2026 11 "Loader Report nov 2026"`.
* Register the spreadsheet for the loaders: `/set_sheet 1 5 11 <URL>`.
* The bot finds each table by its "Date" header, takes the loader from the title, and reads, matches
  and writes **only inside that table**. Another loader's rows are never matched or written. A loader
  without a titled table in a shared tab is blocked ("none titled 'Loader 05'"), and so is a title used
  twice. A tab with a single table still works without a title, unless the title names a different
  loader (e.g. a tab copied from another loader).
* Other blocks in the tab that have their own *Date* column but are not shift reports (e.g.
  *Loader 11 Card Details*: Date, fuel, trips per shift, no per-shift weight/avg) are **ignored**, even
  when their title names a loader. A table counts only if its headers have Trips, Weight and Avg for
  all three shifts plus the Day Total columns.
* `/check_sheet 5 2026 11` shows which table was picked (title, rows, columns), its columns, and any
  ignored blocks.
* The bot always reads the whole tab, however far right or down the tables go. (Older versions had
  `SHEET_READ_MAX_ROWS` / `SHEET_READ_LAST_COLUMN`; they are no longer used and are ignored if still set.)

### Trip card / HSD messages

Trip counts from the operator's trip card and the HSD tanker fills are typed into one short
message (on WhatsApp, then forwarded, or straight to the bot) and saved in the loader's card block,
for example *Loader 11 Card Details* (columns Date, HSD Fuel (L), 1st/2nd/3rd Shift Trips):

```
CARD L11
Date: 06-11-2026
1st: 36
2nd: 37
3rd: 44
HSD: 190 + 154.98
```

* The first line must contain **CARD** and the loader (`L11`, `Loader 11`). The date can be on its
  own line or the first line, with or without the year (`06-11`).
* **Send only what you have.** One shift, two, all three, or only HSD, and only those cells are
  written; the rest of the row is left alone. A day can be filled in as the shifts finish.
  One line works too: `CARD L11 06-11 3rd 44 HSD 154.98`.
* **Trips**: the number is the shift's value. If the cell already has a different number, the
  preview shows "⚠️ replaces 30" before you confirm (same correction policy as reports).
* **HSD**: each tanker fill in litres. Fills that are not in the cell yet are added, and the cell is
  written as a sum (`=190+154.98`) like the hand-typed ones. A fill that is already saved for that
  day is not added again, so re-sending a message, or sending the whole day after a partial one,
  never double counts. Two fills of exactly the same litres must be sent together (`HSD: 150 + 150`).
  A wrong fill is fixed by editing the cell in the sheet.
* The preview compares the card's trips with the CHP report in the loader's shift table
  ("CHP report 21 (card +15)"). Total Trips, Est. Weight and other formulas are never written.
* The card block is found by its title ("Loader NN" plus anything, e.g. *Card Details*) and its
  headers; it must not be a full shift table. Like reports, nothing is written before Confirm, the
  previewed cells are re-checked before writing, and the bot never overwrites another formula.
* `/card_format` shows the format in Telegram.

### Commands

| Command | Who | Purpose |
|---|---|---|
| `/start`, `/help` | everyone | your Telegram ID, role and commands |
| `/my_loaders` | user | loaders you may report for |
| `/card_format` | everyone | how to send trip-card trips and HSD |
| `/my_reports`, `/report <id>` | user | your recent reports, per-loader results |
| `/set_sheet [loader …] <url>` | admin | register / change spreadsheet for one or more loaders; without numbers, for every "Loader NN" tab in the file (validates the URL, checks access, shows each loader's tab) |
| `/list_loaders`, `/loader_info <loader>` | admin | configuration overview |
| `/enable_loader`, `/disable_loader <loader>` | admin | stop/resume updates for a loader |
| `/set_worksheet <loader> <year> <month> "<tab>"` | admin | month → tab mapping (month may be `11` or `nov`) |
| `/remove_worksheet`, `/list_worksheets` | admin | manage mappings |
| `/check_sheet <loader> [year month]` | admin | diagnose tab / headers / date rows |
| `/grant_loader <user_id> <loader…>` | admin | give access (creates the user) |
| `/revoke_loader <user_id> <loader…>` | admin | remove access |
| `/users`, `/disable_user`, `/enable_user <user_id>` | admin | user management |
| `/history [n]` | admin | processing history of all reports |
| `/retry_report <id>` | admin | retry loaders that FAILED (e.g. after sharing a sheet) |
| `/approve_correction`, `/reject_correction <record_id>` | admin | (also inline buttons) |
| `/trust_group [chat_id]`, `/untrust_group`, `/list_groups` | admin | group submissions |

---

## 7. Corrections and duplicates

* **Webhook re-delivery**: `update_id` is the primary key of `telegram_updates`; a repeated delivery
  is acknowledged and ignored.
* **Logical identity**: `(loader, date, shift)` — `shift_entries` stores the latest applied values
  and a revision counter.
* **Same report forwarded twice**: values identical to the sheet → *"Same values are already in the
  sheet"*, nothing to confirm.
* **Different values for a filled shift** (`CORRECTION_POLICY`):
  * `confirm` (default): the summary says *will REPLACE existing 21 trips / 696.380 MT*; Confirm
    replaces the values.
  * `reject`: the loader is blocked.
  * `admin_approval`: after the user confirms, admins get *Approve / Reject* buttons; an admin's own
    corrections need no extra approval.
* **Sheet edited by hand between summary and Confirm**: the loader ends in CONFLICT and nothing is
  written; resend the report to see the current values.
* The audit log (`audit_logs`) records old and new row values of every write, plus every
  configuration change and access decision.

---

## 8. Reliability

* **Ack fast, process durably**: the webhook stores the update and a job in one transaction and
  returns 200; processing happens in the worker. A restart never loses an accepted update.
* **Job queue** in the database (`jobs`): atomic claim, exponential backoff, dead-lettering after
  `JOB_MAX_ATTEMPTS`, stale-lease recovery for crashed workers.
* **Per-loader state machine** (`loader_report_records.processing_status`). Different spreadsheets
  are **not** updated atomically as a group — each loader's result is tracked separately and
  reported honestly (`PARTIAL SUCCESS`).
* **Crash safety**: before writing, the record is set to APPLYING with the row's previous values.
  Writes are absolute values, so re-running after a crash is idempotent and the original
  "previous values" are kept.
* **Concurrency**: a lock per spreadsheet tab (in-process lock + database lease shared by all
  processes) serialises read-modify-write, so two shifts of the same day processed at once cannot
  lose each other's totals. Confirm/Cancel/Approve are compare-and-set transitions (no double apply).
* **Google / Telegram errors**: 429/5xx/network errors are retried with exponential backoff and
  jitter; permission (403), missing spreadsheet (404) and missing tab errors are not retried and are
  reported with the fix ("share with …").
* **Security**: secret-token header checked in constant time; per-loader authorisation on every
  record; admin-only commands; secrets only from the environment; the bot token is never logged.

---

## 9. Deployment

### Do I need a database?

Yes. It holds what you configure through Telegram (loader → spreadsheet URLs, month → tab
mappings, users and their loaders, trusted groups), the reports waiting for Confirm, the job queue
that lets the webhook answer Telegram immediately without losing work, duplicate-update protection,
and the audit log. Without it, everything set with `/set_sheet` or `/grant_loader` would vanish on
every restart.

It does **not** need Docker:

* **Locally**: SQLite, a plain file at `backend/data/app.db`. Nothing to install.
* **On Render**: an external PostgreSQL — currently **Neon** (`DB_URL`). Render's own disk is wiped
  on every deploy and restart, so a SQLite file there would lose data. Any PostgreSQL works (Neon,
  Supabase, RDS, Render Postgres): set `DB_URL` or `DATABASE_URL` (one of them) to the connection
  string as the provider gives it (`postgres://` / `postgresql://`, `?sslmode=require` etc.).
  Pooled endpoints (Neon `-pooler`, PgBouncer, Supabase :6543) are supported: the app turns off
  psycopg's automatic prepared statements, which transaction-mode poolers cannot keep.
* Tables are created on first start; the database user needs permission to create tables.

### Render (native Python, no Docker) — recommended

`render.yaml` at the repository root is a Blueprint for a **Python** web service (pip build +
uvicorn, no Docker) in Render's **Ohio** region, next to the Neon database (AWS us-east-2). Keep the
service and the database in the same region: one report makes a few hundred small queries, which
take milliseconds in-region but over a minute across continents.

1. Push the repository to GitHub/GitLab.
2. Render → *New → Blueprint* → pick the repository. Render reads `render.yaml`.
3. Enter the secret values it asks for. Copy them from `backend/.env`:
   * `DB_URL`: the Neon connection string from `backend/.env`.
   * `GOOGLE_SERVICE_ACCOUNT_JSON`: the JSON **between the single quotes** (starts with `{"type":"service_account"`).
   * `TELEGRAM_WEBHOOK_SECRET`: the generated value.
   * `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_IDS`.
   * `PUBLIC_BASE_URL`: the service URL, e.g. `https://loader-report-bot.onrender.com` (you can fill it in after the first deploy).
4. After the deploy is live, open the service's *Shell* and register the webhook once:

   ```bash
   python scripts/set_webhook.py set
   ```

5. Check `https://<service>.onrender.com/health`, then send `/start` to the bot.

Plans: the web service uses `starter` because the free plan sleeps after 15 idle minutes (Telegram
retries, but the first update after a nap then takes about a minute). Adjust in `render.yaml` if
needed. Keep `numInstances: 1`: the background worker runs inside the web process.

Render takes care of HTTPS, restarts, and log streaming (*Logs* tab; JSON lines). Backups and
point-in-time restore of the data are handled by Neon (see the Neon console for your plan's
retention).

### Docker Compose on AWS EC2 (alternative, with automatic HTTPS)

1. EC2 instance (t3.small is plenty), Docker + Compose plugin installed; security group open on
   80 and 443; a DNS record (e.g. `bot.example.com`) pointing at the instance.
2. Copy `backend/` to the server, create `.env` and `secrets/google-service-account.json`.
3. Start the bot and Caddy (Caddy obtains a Let's Encrypt certificate and exposes only
   `/telegram/webhook` and `/health`):

   ```bash
   DOMAIN=bot.example.com docker compose --profile https up -d --build
   ```

4. Register the webhook once:

   ```bash
   docker compose exec bot python scripts/set_webhook.py set
   ```

The SQLite database lives in the `bot_data` volume. Back it up, e.g. nightly:

```bash
docker compose exec bot python -c "import sqlite3; s=sqlite3.connect('/app/data/app.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d)"
```

### Production process

`uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1 --proxy-headers` (Render's start
command and the Docker `CMD`).
The worker runs as a thread in that process (`RUN_WORKER_IN_PROCESS=true`). Keep one Uvicorn worker
with SQLite. To scale out, move to PostgreSQL (`DATABASE_URL=postgresql+psycopg://…`), set
`RUN_WORKER_IN_PROCESS=false` on the web processes and run `python -m app.worker.runner` separately.

### Health, logging, monitoring

* `GET /health` → `{"status": "ok"|"degraded", "database": "ok", "worker": {...}, "jobs": {...}}`;
  503 if the database is unreachable. `degraded` means the worker heartbeat is stale or some jobs are
  DEAD. Point an uptime monitor (or the ALB target group / Render health check) at it.
* Logs are JSON lines on stdout (`LOG_FORMAT=json`) with fields such as `update_id`, `report_id`,
  `record_id`, `loader_number`, `worksheet`. Ship them with CloudWatch agent / Render log streams.
  Useful alerts: `"level": "ERROR"`, `"message": "job failed"`, `"record not applied"`.
* `python scripts/set_webhook.py info` shows Telegram's view, including the last delivery error.

---

## 10. Data model

`users`, `loaders`, `user_loaders`, `loader_worksheets (loader, year, month) unique`,
`trusted_chats`, `telegram_updates (update_id PK)`, `operational_reports (source_update_id unique)`,
`loader_report_records (report, loader_number) unique — status, previous/updated values, error`,
`shift_entries (loader, date, shift) unique`, `jobs`, `resource_locks`, `audit_logs`.
Tables are created at startup (`create_all`).

---

## 11. Testing

`pytest` runs 225 tests with fake Telegram and in-memory Google Sheets: parser variants (shifts,
dates, Nil, PC, duplicates), totals validation, header detection (single/two-tier/merged/moved
columns/not row 1), date-row matching (serials and text), column mapping, averages and totals,
preservation of other cells and formulas, worksheet routing, authorisation per loader, corrections
under all three policies, cancel/expiry/double-confirm, partial failures, transient-error retries,
crash recovery, concurrent updates of one row, job claiming, webhook authentication and idempotency,
and the HTTP clients' retry/error mapping.

To run the suite against PostgreSQL: `TEST_DATABASE_URL=postgresql://… pytest`
(the database is wiped for each test). The suite (147 tests at the time) passed in full on PostgreSQL 16.

---

## 12. Known limitations

* Schema changes are applied with `create_all` (new tables only). Add Alembic before changing
  existing columns in production.
* The bot does not create monthly worksheets; an admin creates the tab and maps it.
* Reports must be forwarded as text (photos/PDFs are rejected with a hint).
