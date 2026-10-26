# AGENTS.md

Discord bot (`discord.py`) that watches configured Discord categories for ticket
channels (managed by Ticket Tool) and posts one reminder N time after a channel
was created. Reminder state persists in PostgreSQL. Also schedules lecture
announcements/reminders via slash commands and integrates the shared TodoTodo
app (Phase 5). Flat module project — no packages, cogs, or service layers.

- `main.py` — `RGAAssistant(discord.Client)`: event handlers, ticket reminder
  loop, embed builders, `/rga help` group, `CommandTree` sync, and pure helpers
  `is_monitored_channel` / `is_reminder_cancel_message` / `build_help_embed`.
- `lecture.py` — Phase 3 lecture schedule: pure helpers
  (`parse_start_time`, `has_lecture_permission`, embed builders), the
  `/lecture` `app_commands.Group`, and `LectureScheduler` (30s `tasks.loop`).
- `todotodo.py` — Phase 5 TodoTodo integration: `TodoTodoClient` (REST API
  wrapper with exponential-backoff retries on 5xx; never logs the token or
  URL), `TodoCommands` (`/todo decide`), and `TodoRegistrationScheduler`
  (60s `tasks.loop` for TODO auto-registration).
- `embeds.py` — shared embed colors (`COLOR_*`), `truncate`, `error_embed`,
  `success_embed`. Every bot message/response is an embed; only the operations
  mention is plain `content`.
- `config.py` — env parsing into a frozen `Config`.
- `database.py` — `ReminderDatabase` (tickets), `LectureDatabase` (lectures),
  and `TodoDatabase` (TodoTodo mapping/decisions), the only PostgreSQL access
  layer. All share one async `AsyncConnectionPool` (`open_pool`).
- `tools/retry.py` — async CLI (`asyncio.run`) to reset failed ticket deliveries
  (`error` → `pending`).
- `tools/purge.py` — async CLI that deletes finished rows older than
  `RGA_RETENTION_DAYS` (90 by default); `--days` / `--dry-run` override it.
- `tools/migrate_sqlite.py` — idempotent async CLI that copies the legacy SQLite
  state into PostgreSQL (`ON CONFLICT DO NOTHING`).
- `deploy/rga-assistant.service` — systemd unit template.
- `specification.md` — aspirational multi-phase spec. **Phases 2 (ticket
  reminder), 3 (lecture schedule), 4 (application image notification /
  application status), and 5 (TodoTodo integration) are implemented.** Inquiry
  classification (the remaining Phase 4 item) does NOT exist. Don't scaffold
  it unless asked.

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
- DB-backed tests connect to `RGA_TEST_DATABASE_URL` and are **skipped** when it is
  unset. They truncate tables in each test, so point it at a disposable database.
- No linter, formatter, typecheck, or CI is configured; `discord.py`,
  `python-dotenv`, and `psycopg[binary,pool]` are the only deps. Requires Python
  3.10+ (`int | None` syntax).

## Config (`.env` + `.setting`, both KEY=VALUE, loaded by `load_env_files()` in `config.py`)

- **Secrets live in `.env`**: `DISCORD_TOKEN`, `RGA_DATABASE_URL`,
  `RGA_TEST_DATABASE_URL`, `RGA_JEV_API_KEY`, `RGA_TODO_BASE_URL`,
  `RGA_TODO_LIST_TOKEN`. **Deployment-specific settings live in `.setting`**:
  everything else below. Templates: `.env.example` / `.setting.example`
  (`.env` and `.setting` are gitignored).
- Load order matters: OS environment variables win over both files, and
  `.setting` wins over `.env` for duplicated keys (don't duplicate keys).
  Missing files are fine; required keys missing → startup error.
- Required: `DISCORD_TOKEN` (in `.env`), `RGA_TICKET_CATEGORY_IDS`.
  `RGA_REMINDER_TRIGGER_TEXT` is required only when `RGA_REMINDER_CANCEL_TRIGGER`
  is `keyword`.
- Optional: `RGA_EXCLUDED_CHANNEL_IDS`; interval via `RGA_REMINDER_VALUE`
  (default `14`) + `RGA_REMINDER_UNIT` (`seconds|minutes|hours|days|weeks`,
  default `days`).
- Cancel condition: `RGA_REMINDER_CANCEL_TRIGGER` (`image` default | `keyword`,
  mutually exclusive). `image` cancels on a user message with an image file
  attachment; `keyword` cancels on an exact match of `RGA_REMINDER_TRIGGER_TEXT`
  (comma-separated; required only in `keyword` mode, ignored otherwise).
- Destination: `RGA_REMINDER_TARGET` (`ticket` default | `operations` | `both`),
  `RGA_OPERATIONS_CHANNEL_ID` (required for `operations`/`both`; also the failure
  notification target), `RGA_OPERATIONS_MENTION_ROLE_ID` or
  `RGA_OPERATIONS_MENTION_USER_ID` (mutually exclusive).
- Application image notifications can be routed per ticket category via
  `RGA_APPLICATION_NOTIFY_CHANNELS` (`category_id:channel_id` pairs, comma
  separated; duplicate categories rejected; unmapped categories fall back to
  `RGA_OPERATIONS_CHANNEL_ID`). The 14-day operations reminder and failure
  notifications always use `RGA_OPERATIONS_CHANNEL_ID`.
- Retry policy: `RGA_RETRY_BASE_SECONDS` (60), `RGA_RETRY_MAX_SECONDS` (21600),
  `RGA_RETRY_MAX_ATTEMPTS` (6).
- Lectures: `RGA_LECTURE_ROLE_IDS` (comma-separated; empty = administrators only),
  `RGA_LECTURE_REMINDER_MINUTES` (default 15), `RGA_LECTURE_ANNOUNCE_CHANNEL_ID`
  (default `/lecture create` post target; per-lecture override allowed).
- TodoTodo: `RGA_TODO_BASE_URL` + `RGA_TODO_LIST_TOKEN` (both in `.env`; set
  together or the integration stays off — a half-configured state is a config
  error), `RGA_TODO_ROLE_IDS` (comma-separated; empty = administrators only),
  `RGA_TODO_EXCLUDED_USER_IDS` (comma-separated; may not run `/todo decide`
  and dropped from the threshold denominator), `RGA_TODO_DECIDE_CHANNELS`
  (`category_id:channel_id` pairs, comma separated; the only channels from
  which `/todo decide` may run, one per category; unset = the command is
  disabled everywhere), `RGA_TODO_REMIND_CHANNEL_ID` (single channel for the
  daily deadline reminder; unset = reminder off), `RGA_TODO_REMIND_TIME`
  (`HH:MM` JST, default 19:00).
- Legacy vars still read: `RGA_TICKET_CATEGORY_ID`, `RGA_REMINDER_DAYS`.
- Invalid config exits with a message. Discord **Message Content Intent** and
  **Server Members Intent** must be
  enabled in the Developer Portal (Message Content is needed because the cancel
  trigger reads attachments, which Discord strips without it; Server Members is
  needed for the `/todo decide` threshold counting), otherwise `client.run`
  raises
  `PrivilegedIntentsRequired` (caught and logged in Japanese).
- Don't read `.env` or `.setting` (real per-deployment files); use
  `.env.example` / `.setting.example` as reference.

## Behavior gotchas

- Reminders are registered on bot startup (`on_ready` scan), channel create, and
  channel update that moves a channel into a monitored category. There is **no
  periodic re-scan**.
- Due time = `channel.created_at` + interval. Stored as `TIMESTAMPTZ` (UTC),
  computed and displayed in JST (UTC+9 hardcoded as `JST` in `main.py`). psycopg
  returns aware `datetime` objects; don't call `datetime.fromisoformat` on them.
- Two-level state. `ticket_reminders.status` is the overall lifecycle:
  `pending → sent|cancelled` (`sending` is legacy/unused). Per-destination state
  lives in `reminder_deliveries` (primary key `channel_id`+`target`):
  `pending → sending → sent|cancelled|error`.
- `claim_due` runs in a transaction with `SELECT ... FOR UPDATE SKIP LOCKED` and
  flips delivery `pending→sending` to
  prevent double sends; `initialize()` resets stuck `sending→pending` on startup.
  A reminder becomes `sent` only once every delivery is `sent` (an `error`
  delivery keeps the reminder `pending` so it can be retried).
- Failure handling is in `fail_delivery`: transient failures back off
  (`base × 2^(attempts-1)`, capped) and retry; permanent ones (`Forbidden`,
  missing channel) go straight to `error` after `retry_max_attempts`. `error`
  deliveries are reset by `python -m tools.retry`.
- Deliveries are created per `target` at **registration time**, so changing
  `RGA_REMINDER_TARGET` only affects newly registered tickets.
- `register` is `INSERT ... ON CONFLICT (channel_id) DO NOTHING`: re-registering an
  existing channel
  (cancelled, sent, or pending) does NOT create or restore a reminder. This is
  load-bearing for the startup/rescan paths. There is **no reopen support**: a
  closed ticket is not reopened; a new ticket is expected instead.
- `reminder_loop` ticks every 5s, so sends can be up to ~5s late.
- Cancellation is selected by `RGA_REMINDER_CANCEL_TRIGGER` and applies at cancel
  check time (mode changes affect already-registered tickets' future checks).
  `image`: a user message with a file attachment whose `content_type` starts with
  `image/` cancels the pending reminder; URL-embed images and stickers don't
  count. `keyword`: an exact, case-sensitive match of any keyword in
  `RGA_REMINDER_TRIGGER_TEXT` (comma-separated, one or more). In both modes bot
  and webhook messages (Ticket Tool postings) are ignored, and the ticket embed
  wording (`build_ticket_reminder_embed`) and `/rga help` text switch with the
  mode. Message Content Intent must stay enabled — Discord strips attachments
  from MESSAGE_CREATE without it.
- Lecture reminders are sent only **before** the start (`RGA_LECTURE_REMINDER_MINUTES`).
  `lectures.reminder_status` is `pending → sending → sent|expired|cancelled`;
  `status` is `scheduled|cancelled`. `claim_due` runs in a transaction and also
  expires `pending|sending` reminders whose `starts_at` has passed, so a restart
  past the start never sends a late reminder. Transient failures retry after 60s
  until the start; permanent ones expire. `initialize()` resets stuck
  `sending→pending`.
- Lecture edits recompute `reminder_due_at` only when `starts_at` changes; an
  already-`sent` reminder stays `sent` (no duplicate). `/lecture list` is public;
  other lecture subcommands require `RGA_LECTURE_ROLE_IDS` (or administrator).
  Lecture messages use JST `YYYY/MM/DD HH:MM` input/output; timestamps stored UTC.
- TodoTodo (Phase 5): enabled only when both `RGA_TODO_BASE_URL` and
  `RGA_TODO_LIST_TOKEN` are set. On startup the bot joins the list as the
  member `RGA Assistant` (cached `assistant_user_id`, joined lazily when
  missing). An application image detection registers **one** TodoTodo item per
  ticket (`register_todo` is `ON CONFLICT DO NOTHING`; immediate attempt plus a
  60s retry loop). `ticket_todos.status` is `pending → registered|error`
  (attempts/`next_attempt_at` backoff like deliveries; both tables cascade from
  `ticket_reminders`, so `tools.purge` removes them together). Client-level
  retries: 5xx and network errors get 3 attempts with 1s/2s backoff; 4xx fails
  immediately. `/todo decide <ticket channel> (choice 承認/却下)` requires
  `RGA_TODO_ROLE_IDS`
  (or administrator); excluded users (`RGA_TODO_EXCLUDED_USER_IDS`, `.setting`,
  comma-separated) may not run it at all and are dropped from the threshold
  denominator. It runs ONLY from the per-category channel configured in
  `RGA_TODO_DECIDE_CHANNELS` (never inside the ticket); the settle target is
  the required `ticket` channel parameter (concurrent applications), and
  responses/memos name the ticket. When decide roles are configured,
  completion is majority rule:
  approval and reject are tallied separately (GROUP BY in
  `TodoDatabase.decision_tallies`) and whichever side reaches
  `decide_threshold(holders)` = ceil(role holders − excluded / 2, min 1) PATCHes
  `status=done` once (guarded by the `decided` flag); holders come from
  `guild.members` (needs `Intents.members` — Server Members Intent) with bots
  skipped. Without decide roles the first decision settles immediately.
  Every accepted decision appends an append-only memo (承認/却下の別・
  判断人数・実行者; a changed decision is recorded as 却下から承認に変更
  style, threshold mode shows 判断人数 as X/Y). Re-running the same decision
  replies 「もう判定しました」 and touches nothing. Decisions are stored in
  `ticket_todo_decisions` (PK channel+user, last-write-wins) and 判断人数
  counts distinct judges. **The list token is in every API URL — never log
  URLs or raw aiohttp exception messages.**
- TodoTodo deadline reminder (`TodoReminderScheduler`, 60s loop): when
  `RGA_TODO_REMIND_CHANNEL_ID` is set, each tick GETs the list
  (`TodoTodoClient.list_todos`) and (1) once a day at `RGA_TODO_REMIND_TIME`
  (JST) posts the deadline digest via `collect_deadline_reminders`
  (unfinished todos whose `deadline` JST date is tomorrow = 期限前日, or
  past = 期限切れ with day count), and (2) announces todos newly created on
  the TodoTodo side (silent, `build_new_todo_embed`). Bot-created items are
  excluded via the assistant member id (`userId`/`assigneeId`); seen ids are
  persisted in `todo_seen_todos` (`TodoDatabase.claim_new_todos` /
  `seen_todo_count`; the first run on an empty table seeds everything
  silently — never notify on bootstrap). The digest's last-sent date is
  in-memory only: a restart after the send time can repost the digest once.
  Fetch failures retry next minute; a Discord send failure is not retried.
- Commands are global-synced in `setup_hook`; invites need the
  `applications.commands` scope.
- psycopg's async connections refuse Windows' default `ProactorEventLoop`. Call
  `database.ensure_compatible_event_loop_policy()` **before** the loop starts
  (`main()`, the `tools/` CLIs, and the DB test modules do). Without it the pool
  times out at startup on Windows.
- DB-backed tests need a real PostgreSQL; there is no SQLite fallback. Rows are
  copied off the legacy SQLite file with `python -m tools.migrate_sqlite`.
- User-facing strings, README, and spec are Japanese — keep them Japanese.
