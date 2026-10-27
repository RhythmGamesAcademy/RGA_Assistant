import os
import unittest
from datetime import datetime, timedelta, timezone

from database import (
    MeetingLogDatabase,
    ReminderDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)

ensure_compatible_event_loop_policy()

TEST_DATABASE_URL = os.getenv("RGA_TEST_DATABASE_URL")

requires_database = unittest.skipUnless(
    TEST_DATABASE_URL,
    "RGA_TEST_DATABASE_URL is not set; skipping PostgreSQL-backed tests.",
)


async def bootstrap_schema(pool) -> None:
    """Create the schema when missing and empty the data tables for isolation."""
    bootstrap = ReminderDatabase(pool, reminder_after=timedelta(days=14))
    await bootstrap.initialize()
    async with pool.connection() as connection:
        await connection.execute(
            "TRUNCATE ticket_reminders, reminder_deliveries, lectures, meeting_logs CASCADE"
        )


@requires_database
class MeetingLogDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        await bootstrap_schema(self.pool)
        self.database = MeetingLogDatabase(self.pool)
        await self.database.initialize()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_record_and_latest_log(self) -> None:
        await self.database.record(
            10, 20, 101, "議事録1", datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        )
        await self.database.record(
            10, 20, 102, "議事録2", datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc)
        )
        row = await self.database.latest_log(10, 20)
        assert row is not None
        self.assertEqual(row["message_id"], "102")
        self.assertEqual(row["summary"], "議事録2")
        self.assertEqual(
            row["covered_until"],
            datetime(2026, 10, 26, 13, 0, tzinfo=timezone.utc),
        )

    async def test_latest_log_returns_none_for_unknown_channel(self) -> None:
        self.assertIsNone(await self.database.latest_log(10, 99))

    async def test_latest_log_filters_by_channel(self) -> None:
        await self.database.record(
            10, 20, 101, "s", datetime.now(timezone.utc)
        )
        self.assertIsNone(await self.database.latest_log(10, 30))


if __name__ == "__main__":
    unittest.main()
