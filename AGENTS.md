# AGENTS.md

Discord bot (`discord.py`) that watches configured Discord categories for ticket
channels (managed by Ticket Tool) and posts one reminder N time after a channel
was created. Reminder state persists in SQLite. Flat 3-module project — no
packages, cogs, or service layers.

- `main.py` — `RGAAssistant(discord.Client)`: event handlers, reminder loop,
  message builders, and pure helpers `is_monitored_channel` /
  `is_reminder_cancel_message`.
- `config.py` — env parsing into a frozen `Config`.
- `database.py` — `ReminderDatabase`, the only SQLite access layer.
- `tools/retry.py` — CLI to reset failed deliveries (`error` → `pending`).
- `deploy/rga-assistant.service` — systemd unit template.
- `specification.md` — aspirational multi-phase spec. **Only the ticket-reminder
  feature (its Phase 2) is implemented.** Lecture schedule, inquiry
  classification, and task-management integration do NOT exist. Don't scaffold
  them unless asked.

## Environment and commands

Run everything from the repo root. Modules are imported top-level
(`from config import ...`, `from main import ...`), not as a package, so tests
fail if run from another directory.

- The `.venv/` here is a **Windows** venv (`Scripts/`, `pyvenv.cfg`), and the
  system `python` in WSL has none of the deps installed. Use the venv
  interpreter directly:
  - `.venv/Scripts/python.exe main.py`
  - `.venv/Scripts/python.exe -m unittest discover -s tests`
- Recreate elsewhere with `python -m venv .venv` + `pip install -r requirements.txt`.
- Single test: `.venv/Scripts/python.exe -m unittest tests.test_database.ReminderDatabaseTests.test_registers_once_and_claims_when_due`
- No linter, formatter, typecheck, or CI is configured; `discord.py` and
  `python-dotenv` are the only deps. Requires Python 3.10+ (`int | None` syntax).

## Config (`.env`, loaded by python-dotenv in `load_config`)

- Required: `DISCORD_TOKEN`, `RGA_TICKET_CATEGORY_IDS`, `RGA_REMINDER_TRIGGER_TEXT`.
- Optional: `RGA_EXCLUDED_CHANNEL_IDS`; interval via `RGA_REMINDER_VALUE`
  (default `14`) + `RGA_REMINDER_UNIT` (`seconds|minutes|hours|days|weeks`,
  default `days`); `RGA_DATABASE_PATH` (default `data/rga_assistant.sqlite3`).
- Destination: `RGA_REMINDER_TARGET` (`ticket` default | `operations` | `both`),
  `RGA_OPERATIONS_CHANNEL_ID` (required for `operations`/`both`; also the failure
  notification target), `RGA_OPERATIONS_MENTION_ROLE_ID`.
- Retry policy: `RGA_RETRY_BASE_SECONDS` (60), `RGA_RETRY_MAX_SECONDS` (21600),
  `RGA_RETRY_MAX_ATTEMPTS` (6).
- `RGA_REOPEN_TRIGGER_TEXT` (empty = disabled) is the interim reopen detector.
- Legacy vars still read: `RGA_TICKET_CATEGORY_ID`, `RGA_REMINDER_DAYS`.
- Invalid config exits with a message. Discord **Message Content Intent** must be
  enabled in the Developer Portal, otherwise `client.run` raises
  `PrivilegedIntentsRequired` (caught and logged in Japanese).

## Behavior gotchas

- Reminders are registered on bot startup (`on_ready` scan), channel create, and
  channel update that moves a channel into a monitored category. There is **no
  periodic re-scan**.
- Due time = `channel.created_at` + interval. Stored in UTC in SQLite, computed
  and displayed in JST (UTC+9 hardcoded as `JST` in `main.py`).
- Two-level state. `ticket_reminders.status` is the overall lifecycle:
  `pending → sent|cancelled` (`sending` is legacy/unused). Per-destination state
  lives in `reminder_deliveries` (primary key `channel_id`+`target`):
  `pending → sending → sent|cancelled|error`.
- `claim_due` uses `BEGIN IMMEDIATE` and flips delivery `pending→sending` to
  prevent double sends; `initialize()` resets stuck `sending→pending` on startup.
  A reminder becomes `sent` only once every delivery is `sent` (an `error`
  delivery keeps the reminder `pending` so it can be retried).
- Failure handling is in `fail_delivery`: transient failures back off
  (`base × 2^(attempts-1)`, capped) and retry; permanent ones (`Forbidden`,
  missing channel) go straight to `error` after `retry_max_attempts`. `error`
  deliveries are reset by `python -m tools.retry`.
- Deliveries are created per `target` at **registration time**, so changing
  `RGA_REMINDER_TARGET` only affects newly registered tickets.
- `register` is `INSERT OR IGNORE`: re-registering an existing channel
  (cancelled, sent, or pending) does NOT create or restore a reminder. This is
  load-bearing for the startup/rescan paths. `reopen()` is the explicit path that
  resets `opened_at`/`due_at` and rebuilds deliveries.
- `reminder_loop` ticks every 5s, so sends can be up to ~5s late.
- An exact, case-sensitive match of `RGA_REMINDER_TRIGGER_TEXT` cancels the
  pending reminder; the same match on `RGA_REOPEN_TRIGGER_TEXT` reopens it. Bot
  messages and partial matches are ignored.
- User-facing strings, README, and spec are Japanese — keep them Japanese.
