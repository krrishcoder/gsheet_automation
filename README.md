# gsheet_automation

Telegram bot that turns forwarded WhatsApp loader reports into Google Sheets updates — one
spreadsheet per loader, the right monthly tab, date row and shift columns, with averages and day
totals recalculated.

The application lives in [`backend/`](backend/); setup, deployment (Render, no Docker) and
operations are in [`backend/README.md`](backend/README.md). `render.yaml` is the Render Blueprint.
