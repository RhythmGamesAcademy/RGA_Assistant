"""Copy the legacy SQLite state into PostgreSQL.

Usage (run from the repository root, after setting ``RGA_DATABASE_URL``):

    python -m tools.migrate_sqlite
    python -m tools.migrate_sqlite data/rga_assistant.sqlite3

The script copies ``ticket_reminders``, ``reminder_deliveries``, and ``lectures``.
It is idempotent: existing rows are left untouched (``ON CONFLICT DO NOTHING``), so
running it again never duplicates or resets state. Run it once before starting the
bot on the new database, otherwise the startup scan re-registers still-open tickets
and reminders that were already sent may be sent again.
"""

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import Config, load_config
from database import (
    LectureDatabase,
    ReminderDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)

logger = logging.getLogger("rga_assistant")

DEFAULT_SOURCE = Path("data/rga_assistant.sqlite3")

# Tables are migrated in this order because deliveries reference reminders.
TABLES = ("ticket_reminders", "reminder_deliveries", "lectures")

TIMESTAMP_COLUMNS = {
    "ticket_reminders": {"opened_at", "due_at", "sent_at", "updated_at"},
    "reminder_deliveries": {"next_attempt_at", "sent_at", "updated_at"},
    "lectures": {
        "starts_at",
        "reminder_due_at",
        "next_attempt_at",
        "reminder_sent_at",
        "created_at",
        "updated_at",
    },
}

# Legacy databases (schema v1) predate the per-destination table; a ticket-level
# status is mapped onto each configured target.
LEGACY_DELIVERY_STATUS = {
    "pending": "pending",
    "sending": "pending",
    "sent": "sent",
    "cancelled": "cancelled",
}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    )
    return cursor.fetchone() is not None


def _read_rows(connection: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return connection.execute(f"SELECT * FROM {table}").fetchall()


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = (
        value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _convert(table: str, column: str, value: Any) -> Any:
    if column in TIMESTAMP_COLUMNS[table]:
        return _to_datetime(value)
    return value


async def _target_columns(connection, table: str) -> list[str]:
    cursor = await connection.execute(f"SELECT * FROM {table} LIMIT 0")
    return [column.name for column in cursor.description]


async def _insert_rows(
    connection,
    table: str,
    rows: Iterable[sqlite3.Row],
    allowed_channel_ids: set[str] | None = None,
) -> tuple[int, int]:
    """Insert rows and return ``(inserted, skipped)``."""
    target_columns = await _target_columns(connection, table)

    inserted = 0
    skipped = 0
    for row in rows:
        available = row.keys()
        columns = [column for column in target_columns if column in available]
        if not columns:
            skipped += 1
            continue
        if (
            allowed_channel_ids is not None
            and row["channel_id"] not in allowed_channel_ids
        ):
            skipped += 1
            continue
        values = tuple(_convert(table, column, row[column]) for column in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        cursor = await connection.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            "ON CONFLICT DO NOTHING",
            values,
        )
        if cursor.rowcount:
            inserted += 1
        else:
            skipped += 1
    return inserted, skipped


async def _backfill_deliveries(
    connection, source: sqlite3.Connection, targets: tuple[str, ...]
) -> int:
    """Synthesize delivery rows for legacy databases without that table."""
    inserted = 0
    for row in _read_rows(source, "ticket_reminders"):
        status = LEGACY_DELIVERY_STATUS.get(row["status"], "pending")
        for target in targets:
            cursor = await connection.execute(
                """
                INSERT INTO reminder_deliveries
                    (channel_id, target, status, attempts, updated_at)
                VALUES (%s, %s, %s, 0, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    row["channel_id"],
                    target,
                    status,
                    _to_datetime(row["updated_at"]) or datetime.now(timezone.utc),
                ),
            )
            inserted += cursor.rowcount
    return inserted


async def migrate(
    pool,
    source_path: Path,
    targets: tuple[str, ...] = ("ticket",),
) -> dict[str, int]:
    if not source_path.exists():
        raise FileNotFoundError(f"SQLiteデータベースが見つかりません: {source_path}")

    inserted: dict[str, int] = {}
    skipped: dict[str, int] = {}
    with sqlite3.connect(source_path) as source:
        source.row_factory = sqlite3.Row
        async with pool.connection() as connection:
            for table in TABLES:
                if not _table_exists(source, table):
                    continue
                allowed = None
                if table == "reminder_deliveries":
                    cursor = await connection.execute(
                        "SELECT channel_id FROM ticket_reminders"
                    )
                    allowed = {row["channel_id"] for row in await cursor.fetchall()}
                count, skip = await _insert_rows(
                    connection, table, _read_rows(source, table), allowed
                )
                inserted[table] = count
                skipped[table] = skip
            if _table_exists(source, "ticket_reminders") and not _table_exists(
                source, "reminder_deliveries"
            ):
                inserted["reminder_deliveries"] = await _backfill_deliveries(
                    connection, source, targets
                )
                skipped["reminder_deliveries"] = 0

    for table in TABLES:
        if table in inserted:
            logger.info(
                "%s: 取り込み %s件 / スキップ %s件",
                table,
                inserted[table],
                skipped.get(table, 0),
            )
    return inserted


async def run(args: argparse.Namespace, config: Config) -> int:
    pool = await open_pool(config.database_url)
    try:
        # Make sure the destination schema exists before copying rows into it.
        await ReminderDatabase(
            pool,
            config.reminder_after,
            config.reminder_targets,
        ).initialize()
        await LectureDatabase(pool).initialize()

        inserted = await migrate(pool, Path(args.source), config.reminder_targets)
        total = sum(inserted.values())
        print(f"{total}件の行をPostgreSQLへ取り込みました。")
        return 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="旧SQLiteデータベースの内容をPostgreSQLへ移行します。"
        " 既存の行は変更せず、再実行しても安全です。"
    )
    parser.add_argument(
        "source",
        nargs="?",
        default=str(DEFAULT_SOURCE),
        help=f"移行元のSQLiteファイル（既定: {DEFAULT_SOURCE}）。",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ensure_compatible_event_loop_policy()

    try:
        config = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        return asyncio.run(run(args, config))
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
