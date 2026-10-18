import os
import unittest
from datetime import datetime, timedelta, timezone

from database import (
    LectureDatabase,
    ReminderDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)

ensure_compatible_event_loop_policy()

JST = timezone(timedelta(hours=9))
STARTS_AT = datetime(2026, 10, 18, 15, 0, tzinfo=JST)
DUE_AT = STARTS_AT.astimezone(timezone.utc) - timedelta(minutes=15)
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
            "TRUNCATE ticket_reminders, reminder_deliveries, lectures CASCADE"
        )


@requires_database
class LectureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        await bootstrap_schema(self.pool)
        self.database = LectureDatabase(self.pool)
        await self.database.initialize()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def _create(
        self, starts_at: datetime = STARTS_AT, lead_minutes: int = 15
    ) -> dict:
        return await self.database.create(
            20,
            title="基礎リズム論",
            lecturer="講師A",
            description="説明",
            starts_at=starts_at,
            announce_channel_id=30,
            reminder_lead_minutes=lead_minutes,
            created_by=40,
        )

    async def test_create_stores_utc_and_computes_due_at(self) -> None:
        lecture = await self._create()

        self.assertEqual(len(lecture["lecture_id"]), LectureDatabase.ID_LENGTH)
        self.assertEqual(lecture["status"], "scheduled")
        self.assertEqual(lecture["reminder_status"], "pending")
        self.assertEqual(lecture["starts_at"], STARTS_AT.astimezone(timezone.utc))
        self.assertEqual(lecture["reminder_due_at"], DUE_AT)

    async def test_create_allocates_unique_ids(self) -> None:
        ids = {(await self._create())["lecture_id"] for _ in range(20)}
        self.assertEqual(len(ids), 20)

    async def test_claim_due_only_after_the_lead_time(self) -> None:
        lecture = await self._create()

        self.assertEqual(
            await self.database.claim_due(DUE_AT - timedelta(seconds=1)), []
        )
        claimed = await self.database.claim_due(DUE_AT)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["lecture_id"], lecture["lecture_id"])
        self.assertEqual(
            (await self.database.get(lecture["lecture_id"]))["reminder_status"],
            "sending",
        )
        # Already claimed, so it is not returned twice.
        self.assertEqual(
            await self.database.claim_due(DUE_AT + timedelta(minutes=1)), []
        )

    async def test_reminder_is_expired_once_the_lecture_started(self) -> None:
        lecture = await self._create()

        self.assertEqual(await self.database.claim_due(STARTS_AT), [])
        self.assertEqual(
            (await self.database.get(lecture["lecture_id"]))["reminder_status"],
            "expired",
        )

    async def test_lecture_registered_shortly_before_start_still_reminds(self) -> None:
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        lecture = await self._create(starts_at=starts_at, lead_minutes=15)

        claimed = await self.database.claim_due()
        self.assertEqual(
            [row["lecture_id"] for row in claimed], [lecture["lecture_id"]]
        )

    async def test_transient_failure_is_retried_after_the_delay(self) -> None:
        lecture = await self._create()
        await self.database.claim_due(DUE_AT)

        await self.database.release_reminder(
            lecture["lecture_id"], "boom", retry_seconds=60, now=DUE_AT
        )
        self.assertEqual(
            (await self.database.get(lecture["lecture_id"]))["next_attempt_at"],
            DUE_AT + timedelta(seconds=60),
        )
        self.assertEqual(
            await self.database.claim_due(DUE_AT + timedelta(seconds=59)), []
        )
        self.assertEqual(
            len(await self.database.claim_due(DUE_AT + timedelta(seconds=60))), 1
        )

    async def test_permanent_failure_expires_the_reminder(self) -> None:
        lecture = await self._create()
        await self.database.claim_due(DUE_AT)

        await self.database.expire_reminder(
            lecture["lecture_id"], "missing permission"
        )
        record = await self.database.get(lecture["lecture_id"])
        self.assertEqual(record["reminder_status"], "expired")
        self.assertEqual(record["last_error"], "missing permission")

    async def test_mark_reminder_sent_stores_the_message(self) -> None:
        lecture = await self._create()
        await self.database.claim_due(DUE_AT)

        await self.database.mark_reminder_sent(lecture["lecture_id"], 555)
        record = await self.database.get(lecture["lecture_id"])
        self.assertEqual(record["reminder_status"], "sent")
        self.assertEqual(record["reminder_message_id"], "555")
        self.assertIsNotNone(record["reminder_sent_at"])

    async def test_cancel_cancels_the_pending_reminder(self) -> None:
        lecture = await self._create()

        cancelled = await self.database.cancel(lecture["lecture_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["reminder_status"], "cancelled")
        self.assertEqual(await self.database.claim_due(DUE_AT), [])

    async def test_cancel_after_sent_keeps_the_sent_state(self) -> None:
        lecture = await self._create()
        await self.database.claim_due(DUE_AT)
        await self.database.mark_reminder_sent(lecture["lecture_id"], 555)

        cancelled = await self.database.cancel(lecture["lecture_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["reminder_status"], "sent")

    async def test_update_recomputes_the_due_at_only_with_a_new_start(self) -> None:
        lecture = await self._create()

        renamed = await self.database.update(
            lecture["lecture_id"], title="応用リズム論"
        )
        self.assertEqual(renamed["title"], "応用リズム論")
        self.assertEqual(renamed["reminder_due_at"], DUE_AT)

        moved_start = STARTS_AT + timedelta(days=1)
        moved = await self.database.update(
            lecture["lecture_id"], starts_at=moved_start
        )
        self.assertEqual(
            moved["reminder_due_at"],
            moved_start.astimezone(timezone.utc) - timedelta(minutes=15),
        )

    async def test_update_after_sent_does_not_reset_the_reminder(self) -> None:
        lecture = await self._create()
        await self.database.claim_due(DUE_AT)
        await self.database.mark_reminder_sent(lecture["lecture_id"], 555)

        updated = await self.database.update(lecture["lecture_id"], title="変更後")
        self.assertEqual(updated["reminder_status"], "sent")
        self.assertEqual(
            await self.database.claim_due(DUE_AT + timedelta(days=1)), []
        )

    async def test_delete_removes_the_lecture(self) -> None:
        lecture = await self._create()

        self.assertTrue(await self.database.delete(lecture["lecture_id"]))
        self.assertIsNone(await self.database.get(lecture["lecture_id"]))
        self.assertFalse(await self.database.delete(lecture["lecture_id"]))

    async def test_list_orders_by_start_and_hides_cancelled(self) -> None:
        later = await self._create(starts_at=STARTS_AT + timedelta(days=1))
        sooner = await self._create()
        cancelled = await self._create(starts_at=STARTS_AT + timedelta(days=2))
        await self.database.cancel(cancelled["lecture_id"])

        listed = await self.database.list_lectures(20)
        self.assertEqual(
            [row["lecture_id"] for row in listed],
            [sooner["lecture_id"], later["lecture_id"]],
        )

        with_cancelled = await self.database.list_lectures(20, include_cancelled=True)
        self.assertEqual(len(with_cancelled), 3)

    async def test_sending_reminder_is_restored_after_restart(self) -> None:
        lecture = await self._create()
        await self.database.claim_due(DUE_AT)

        restarted = LectureDatabase(self.pool)
        await restarted.initialize()

        self.assertEqual(
            (await restarted.get(lecture["lecture_id"]))["reminder_status"],
            "pending",
        )
        self.assertEqual(len(await restarted.claim_due(DUE_AT)), 1)

    async def test_purge_old_removes_only_finished_lectures(self) -> None:
        done = await self._create()
        await self.database.claim_due(DUE_AT)
        await self.database.mark_reminder_sent(done["lecture_id"], 555)
        cancelled = await self._create(starts_at=STARTS_AT + timedelta(days=1))
        await self.database.cancel(cancelled["lecture_id"])
        pending = await self._create(starts_at=STARTS_AT + timedelta(days=2))

        cutoff = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertEqual(await self.database.count_purgeable(cutoff), 2)
        # A cutoff in the past leaves every row in place.
        self.assertEqual(
            await self.database.purge_old(STARTS_AT - timedelta(days=365)), 0
        )

        self.assertEqual(await self.database.purge_old(cutoff), 2)
        self.assertIsNone(await self.database.get(done["lecture_id"]))
        self.assertIsNone(await self.database.get(cancelled["lecture_id"]))
        self.assertIsNotNone(await self.database.get(pending["lecture_id"]))


if __name__ == "__main__":
    unittest.main()
