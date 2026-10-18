import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import (
    LectureDatabase,
    ReminderDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)
from tools.migrate_sqlite import migrate

ensure_compatible_event_loop_policy()

OPENED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
JST = timezone(timedelta(hours=9))
STARTS_AT = datetime(2026, 10, 18, 15, 0, tzinfo=JST)
TEST_DATABASE_URL = os.getenv("RGA_TEST_DATABASE_URL")

requires_database = unittest.skipUnless(
    TEST_DATABASE_URL,
    "RGA_TEST_DATABASE_URL is not set; skipping PostgreSQL-backed tests.",
)

# The SQLite schema as it existed before the PostgreSQL migration (version 2).
LEGACY_SCHEMA = """
CREATE TABLE ticket_reminders (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'sending', 'sent', 'cancelled')
    ),
    sent_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE reminder_deliveries (
    channel_id TEXT NOT NULL,
    target TEXT NOT NULL CHECK (target IN ('ticket', 'operations')),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'sending', 'sent', 'cancelled', 'error')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    sent_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, target)
);

CREATE TABLE lectures (
    lecture_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    title TEXT NOT NULL,
    lecturer TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    announce_channel_id TEXT NOT NULL,
    reminder_lead_minutes INTEGER NOT NULL,
    reminder_due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'cancelled')),
    reminder_status TEXT NOT NULL CHECK (
        reminder_status IN ('pending', 'sending', 'sent', 'cancelled', 'expired')
    ),
    reminder_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    reminder_sent_at TEXT,
    announcement_message_id TEXT,
    reminder_message_id TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


async def bootstrap_schema(pool) -> None:
    bootstrap = ReminderDatabase(pool, reminder_after=timedelta(days=14))
    await bootstrap.initialize()
    async with pool.connection() as connection:
        await connection.execute(
            "TRUNCATE ticket_reminders, reminder_deliveries, lectures CASCADE"
        )


@requires_database
class MigrateSqliteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        await bootstrap_schema(self.pool)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.source = Path(self.temp_dir.name) / "legacy.sqlite3"
        self.database = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        self.lectures = LectureDatabase(self.pool)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()
        await self.pool.close()

    def _write_legacy(self) -> None:
        opened = OPENED_AT.isoformat()
        due = (OPENED_AT + timedelta(days=14)).isoformat()
        with sqlite3.connect(self.source) as connection:
            connection.executescript(LEGACY_SCHEMA)
            connection.executemany(
                """
                INSERT INTO ticket_reminders
                    (channel_id, guild_id, channel_name, opened_at, due_at,
                     status, sent_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("10", "20", "ticket-10", opened, due, "pending", None, opened),
                    ("11", "20", "ticket-11", opened, due, "sent", opened, opened),
                    ("12", "20", "ticket-12", opened, due, "cancelled", None, opened),
                ],
            )
            connection.executemany(
                """
                INSERT INTO reminder_deliveries
                    (channel_id, target, status, attempts, next_attempt_at,
                     last_error, sent_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("10", "ticket", "pending", 0, None, None, None, opened),
                    ("11", "ticket", "sent", 1, None, None, opened, opened),
                    ("12", "ticket", "cancelled", 0, None, None, None, opened),
                ],
            )
            connection.execute(
                """
                INSERT INTO lectures
                    (lecture_id, guild_id, title, lecturer, description, starts_at,
                     announce_channel_id, reminder_lead_minutes, reminder_due_at,
                     status, reminder_status, reminder_attempts, next_attempt_at,
                     last_error, reminder_sent_at, announcement_message_id,
                     reminder_message_id, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "abc1234567", "20", "基礎リズム論", "講師A", "説明",
                    STARTS_AT.isoformat(), "30", 15,
                    (STARTS_AT - timedelta(minutes=15)).isoformat(),
                    "scheduled", "pending", 0, None, None, None, "999", None,
                    "40", OPENED_AT.isoformat(), OPENED_AT.isoformat(),
                ),
            )

    async def test_copies_every_table(self) -> None:
        self._write_legacy()

        inserted = await migrate(self.pool, self.source)

        self.assertEqual(inserted["ticket_reminders"], 3)
        self.assertEqual(inserted["reminder_deliveries"], 3)
        self.assertEqual(inserted["lectures"], 1)

        self.assertEqual((await self.database.get_reminder("10"))["status"], "pending")
        self.assertEqual((await self.database.get_reminder("11"))["status"], "sent")
        self.assertEqual(
            (await self.database.get_delivery("11", "ticket"))["status"], "sent"
        )
        self.assertEqual(
            (await self.database.get_reminder("12"))["status"], "cancelled"
        )

        # Only the still-pending ticket becomes due.
        due = await self.database.claim_due(OPENED_AT + timedelta(days=14))
        self.assertEqual([row["channel_id"] for row in due], ["10"])

        lecture = await self.lectures.get("abc1234567")
        self.assertEqual(lecture["title"], "基礎リズム論")
        self.assertEqual(lecture["starts_at"], STARTS_AT.astimezone(timezone.utc))
        self.assertEqual(lecture["announcement_message_id"], "999")
        self.assertEqual(lecture["created_by"], "40")

    async def test_rerunning_does_not_duplicate_or_reset_state(self) -> None:
        self._write_legacy()
        await migrate(self.pool, self.source)

        # Cancel one ticket, then migrate again: the PostgreSQL state must win.
        await self.database.cancel(10)
        second = await migrate(self.pool, self.source)

        self.assertEqual(second["ticket_reminders"], 0)
        self.assertEqual(second["lectures"], 0)
        self.assertEqual(
            (await self.database.get_reminder("10"))["status"], "cancelled"
        )
        self.assertEqual(len(await self.lectures.list_lectures(20)), 1)


if __name__ == "__main__":
    unittest.main()
