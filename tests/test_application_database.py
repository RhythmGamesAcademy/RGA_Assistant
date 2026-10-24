import os
import unittest
from datetime import datetime, timedelta, timezone

from database import (
    ApplicationStatusDatabase,
    ReminderDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)

# IsolatedAsyncioTestCase creates a new loop per test, so the policy has to be set
# before the tests run (psycopg cannot use Windows' ProactorEventLoop).
ensure_compatible_event_loop_policy()

OPENED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
DUE_AT = OPENED_AT + timedelta(days=14)
OLD_JUDGED_AT = datetime(2025, 1, 1, tzinfo=timezone.utc)
TEST_DATABASE_URL = os.getenv("RGA_TEST_DATABASE_URL")

requires_database = unittest.skipUnless(
    TEST_DATABASE_URL,
    "RGA_TEST_DATABASE_URL is not set; skipping PostgreSQL-backed tests.",
)


@requires_database
class ApplicationStatusDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        bootstrap = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        await bootstrap.initialize()
        # Reminder rows are fixtures of this module (the purge rule reads
        # ticket_reminders), so the ticket tables are also emptied here.
        # CASCADE covers reminder_deliveries; lectures is left untouched.
        async with self.pool.connection() as connection:
            await connection.execute(
                "TRUNCATE application_status, ticket_reminders CASCADE"
            )
        self.database = ApplicationStatusDatabase(self.pool)

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def _reminder(
        self, channel_id: int = 10
    ) -> ReminderDatabase:
        database = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        await database.register(
            channel_id, 20, f"ticket-{channel_id}", OPENED_AT
        )
        return database

    async def test_get_returns_none_when_absent(self) -> None:
        self.assertIsNone(await self.database.get(1000))

    async def test_save_then_get_round_trips_a_simple_row(self) -> None:
        await self.database.save(10, has_application=True, mode="simple")

        row = await self.database.get(10)
        self.assertTrue(row["has_application"])
        self.assertEqual(row["mode"], "simple")
        self.assertIsNone(row["detail"])
        self.assertIsNotNone(row["judged_at"])

    async def test_save_upserts_and_overwrites_by_channel_id(self) -> None:
        await self.database.save(10, has_application=True, mode="simple")
        await self.database.save(
            10,
            has_application=False,
            mode="jev",
            detail={"application_state": "未申請", "confidence": 0.9},
        )

        row = await self.database.get(10)
        self.assertFalse(row["has_application"])
        self.assertEqual(row["mode"], "jev")
        self.assertEqual(
            row["detail"],
            {"application_state": "未申請", "confidence": 0.9},
        )

        self.assertIsNone(await self.database.get(999))

    async def test_save_stores_unknown_as_null(self) -> None:
        await self.database.save(
            10,
            has_application=None,
            mode="jev",
            detail={"application_state": "判断不能", "confidence": 0.4},
        )

        row = await self.database.get(10)
        self.assertIsNone(row["has_application"])
        self.assertEqual(row["detail"]["application_state"], "判断不能")

    async def test_older_judgment_does_not_overwrite_a_newer_row(self) -> None:
        now = datetime.now(timezone.utc)
        await self.database.save(
            10,
            has_application=True,
            mode="simple",
            judged_at=now,
        )
        await self.database.save(
            10,
            has_application=False,
            mode="jev",
            detail={"application_state": "未申請"},
            judged_at=now - timedelta(hours=1),
        )

        row = await self.database.get(10)
        self.assertTrue(row["has_application"])
        self.assertEqual(row["mode"], "simple")

    async def test_purge_old_keeps_rows_of_open_reminders(self) -> None:
        old = OLD_JUDGED_AT
        future_cutoff = datetime.now(timezone.utc) + timedelta(days=1)

        # pending reminder → the ticket is still open, the row is kept.
        await self._reminder(10)
        await self.database.save(10, has_application=True, mode="simple", judged_at=old)

        # error reminder → it may still be retried and delivered, keep the row.
        errored = await self._reminder(12)
        await errored.claim_due(DUE_AT)
        await errored.fail_delivery("12", "ticket", "boom", permanent=True)
        await self.database.save(12, has_application=None, mode="jev", judged_at=old)

        self.assertEqual(await self.database.count_purgeable(future_cutoff), 0)
        self.assertEqual(await self.database.purge_old(future_cutoff), 0)
        self.assertIsNotNone(await self.database.get(10))
        self.assertIsNotNone(await self.database.get(12))

        # A cutoff in the past leaves every row in place.
        self.assertEqual(await self.database.purge_old(datetime(2024, 1, 1, tzinfo=timezone.utc)), 0)

    async def test_purge_old_removes_rows_of_closed_or_unknown_tickets(self) -> None:
        old = OLD_JUDGED_AT
        future_cutoff = datetime.now(timezone.utc) + timedelta(days=1)

        # cancelled reminder (ticket closed without sending).
        cancelled = await self._reminder(11)
        await cancelled.cancel(11)
        await self.database.save(11, has_application=True, mode="simple", judged_at=old)

        # no reminder row at all (purged earlier or never registered).
        await self.database.save(14, has_application=True, mode="simple", judged_at=old)

        self.assertEqual(await self.database.count_purgeable(future_cutoff), 2)
        self.assertEqual(await self.database.purge_old(future_cutoff), 2)
        self.assertIsNone(await self.database.get(11))
        self.assertIsNone(await self.database.get(14))
        self.assertEqual(await self.database.count_purgeable(future_cutoff), 0)


if __name__ == "__main__":
    unittest.main()