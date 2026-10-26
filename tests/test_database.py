import os
import unittest
from datetime import datetime, timedelta, timezone

from database import (
    SCHEMA_VERSION,
    ReminderDatabase,
    TodoDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)

# IsolatedAsyncioTestCase creates a new loop per test, so the policy has to be set
# before the tests run (psycopg cannot use Windows' ProactorEventLoop).
ensure_compatible_event_loop_policy()

OPENED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
TEST_DATABASE_URL = os.getenv("RGA_TEST_DATABASE_URL")

requires_database = unittest.skipUnless(
    TEST_DATABASE_URL,
    "RGA_TEST_DATABASE_URL is not set; skipping PostgreSQL-backed tests.",
)


async def bootstrap_schema(pool) -> None:
    """Create the schema when missing and empty the data tables for isolation."""
    bootstrap = ReminderDatabase(
        pool,
        reminder_after=timedelta(days=14),
    )
    await bootstrap.initialize()
    async with pool.connection() as connection:
        await connection.execute(
            "TRUNCATE ticket_reminders, reminder_deliveries, "
            "application_image_notifications, ticket_application_status, "
            "lectures, ticket_todos, ticket_todo_decisions CASCADE"
        )


@requires_database
class ReminderDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        await bootstrap_schema(self.pool)

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    def make_database(self, **kwargs) -> ReminderDatabase:
        return ReminderDatabase(
            self.pool,
            reminder_after=timedelta(days=14),
            **kwargs,
        )

    async def test_registers_once_and_claims_when_due(self) -> None:
        database = self.make_database()
        self.assertTrue(await database.register(10, 20, "ticket-10", OPENED_AT))
        self.assertFalse(await database.register(10, 20, "ticket-10", OPENED_AT))

        self.assertEqual(await database.claim_due(OPENED_AT + timedelta(days=13)), [])
        due = await database.claim_due(OPENED_AT + timedelta(days=14))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["channel_id"], "10")
        self.assertEqual(due[0]["target"], "ticket")
        self.assertTrue(await database.is_sending("10", "ticket"))
        self.assertEqual(await database.claim_due(OPENED_AT + timedelta(days=15)), [])
        await database.cancel(10)
        self.assertFalse(await database.is_sending("10", "ticket"))

    async def test_cancelled_ticket_is_not_claimed(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        await database.cancel(10)

        self.assertEqual(await database.claim_due(OPENED_AT + timedelta(days=15)), [])

    async def test_application_image_notification_is_claimed_once(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)

        self.assertTrue(await database.claim_application_image_notification(10))
        self.assertFalse(await database.claim_application_image_notification(10))

        await database.mark_application_image_notification_sent(10)
        self.assertFalse(await database.claim_application_image_notification(10))

    async def test_failed_application_image_notification_can_be_retried(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)

        self.assertTrue(await database.claim_application_image_notification(10))
        await database.release_application_image_notification(10)
        self.assertTrue(await database.claim_application_image_notification(10))

    async def test_registering_existing_cancelled_ticket_does_not_restore_reminder(
        self,
    ) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        await database.cancel(10)
        await database.register(10, 20, "ticket-10", OPENED_AT)

        self.assertEqual(await database.claim_due(OPENED_AT + timedelta(days=15)), [])

    async def test_pending_reminder_is_restored_after_restart(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        await database.claim_due(OPENED_AT + timedelta(days=14))
        restarted = self.make_database()
        await restarted.initialize()

        self.assertEqual(
            len(await restarted.claim_due(OPENED_AT + timedelta(days=14))),
            1,
        )

    async def test_mark_sent_finalizes_reminder(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        await database.claim_due(OPENED_AT + timedelta(days=14))
        await database.mark_delivery_sent("10", "ticket")

        self.assertEqual((await database.get_reminder("10"))["status"], "sent")
        self.assertEqual(
            (await database.get_delivery("10", "ticket"))["status"], "sent"
        )

    async def test_transient_failure_backs_off_then_retries(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        due = OPENED_AT + timedelta(days=14)
        await database.claim_due(due)

        status, attempts = await database.fail_delivery(
            "10", "ticket", "boom", now=due
        )
        self.assertEqual((status, attempts), ("pending", 1))

        delivery = await database.get_delivery("10", "ticket")
        self.assertEqual(delivery["next_attempt_at"], due + timedelta(seconds=60))
        # Not due yet because of the backoff delay.
        self.assertEqual(await database.claim_due(due + timedelta(seconds=59)), [])
        # Claimed again once the backoff elapses.
        self.assertEqual(
            len(await database.claim_due(due + timedelta(seconds=60))),
            1,
        )

    async def test_repeated_failures_end_in_error_state(self) -> None:
        database = self.make_database(
            retry_base_seconds=10,
            retry_max_seconds=10,
            retry_max_attempts=2,
        )
        await database.initialize()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        now = OPENED_AT + timedelta(days=14)

        await database.claim_due(now)
        self.assertEqual(
            (await database.fail_delivery("10", "ticket", "boom", now=now))[0],
            "pending",
        )
        now += timedelta(seconds=10)
        await database.claim_due(now)
        self.assertEqual(
            (await database.fail_delivery("10", "ticket", "boom", now=now))[0],
            "error",
        )

        self.assertEqual(len(await database.list_errors()), 1)
        self.assertEqual(await database.retry_errors(10), 1)
        self.assertEqual(await database.list_errors(), [])
        self.assertEqual(
            (await database.get_delivery("10", "ticket"))["status"], "pending"
        )

    async def test_permanent_failure_goes_straight_to_error(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        due = OPENED_AT + timedelta(days=14)
        await database.claim_due(due)

        status, attempts = await database.fail_delivery(
            "10", "ticket", "missing permission", permanent=True, now=due
        )
        self.assertEqual((status, attempts), ("error", 1))

    async def test_purge_old_removes_only_terminal_reminders(self) -> None:
        database = self.make_database()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        await database.claim_due(OPENED_AT + timedelta(days=14))
        await database.mark_delivery_sent("10", "ticket")
        await database.register(11, 20, "ticket-11", OPENED_AT)  # stays pending

        cutoff = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertEqual(await database.count_purgeable(cutoff), 1)
        # A cutoff in the past leaves every row in place.
        self.assertEqual(await database.purge_old(OPENED_AT), 0)

        self.assertEqual(await database.purge_old(cutoff), 1)
        self.assertIsNone(await database.get_reminder("10"))
        self.assertIsNone(await database.get_delivery("10", "ticket"))
        self.assertIsNotNone(await database.get_reminder("11"))


@requires_database
class MultiTargetReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        await bootstrap_schema(self.pool)
        self.database = ReminderDatabase(
            self.pool,
            reminder_after=timedelta(days=14),
            targets=("ticket", "operations"),
        )
        await self.database.initialize()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_creates_a_delivery_per_target(self) -> None:
        await self.database.register(10, 20, "ticket-10", OPENED_AT)
        due = await self.database.claim_due(OPENED_AT + timedelta(days=14))
        self.assertEqual({row["target"] for row in due}, {"ticket", "operations"})

    async def test_partial_success_only_finalizes_after_both_sent(self) -> None:
        await self.database.register(10, 20, "ticket-10", OPENED_AT)
        await self.database.claim_due(OPENED_AT + timedelta(days=14))

        await self.database.mark_delivery_sent("10", "ticket")
        self.assertEqual(
            (await self.database.get_reminder("10"))["status"], "pending"
        )

        await self.database.mark_delivery_sent("10", "operations")
        self.assertEqual((await self.database.get_reminder("10"))["status"], "sent")

    async def test_cancel_cancels_every_delivery(self) -> None:
        await self.database.register(10, 20, "ticket-10", OPENED_AT)
        await self.database.cancel(10)

        self.assertEqual(
            (await self.database.get_delivery("10", "ticket"))["status"], "cancelled"
        )
        self.assertEqual(
            (await self.database.get_delivery("10", "operations"))["status"],
            "cancelled",
        )


@requires_database
class SchemaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        await bootstrap_schema(self.pool)

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_initialize_is_idempotent_and_records_version(self) -> None:
        database = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        await database.initialize()
        await database.initialize()

        async with self.pool.connection() as connection:
            cursor = await connection.execute("SELECT version FROM schema_version")
            row = await cursor.fetchone()
        self.assertEqual(row["version"], SCHEMA_VERSION)

    async def test_initialize_restores_in_flight_deliveries(self) -> None:
        database = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        await database.initialize()
        await database.register(10, 20, "ticket-10", OPENED_AT)
        await database.claim_due(OPENED_AT + timedelta(days=14))

        restarted = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        await restarted.initialize()

        self.assertFalse(await restarted.is_sending("10", "ticket"))
        self.assertEqual(
            len(await restarted.claim_due(OPENED_AT + timedelta(days=14))),
            1,
        )


@requires_database
class TodoDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = await open_pool(TEST_DATABASE_URL)
        await bootstrap_schema(self.pool)

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    def make_database(self, **kwargs) -> TodoDatabase:
        return TodoDatabase(
            self.pool,
            retry_base_seconds=60.0,
            retry_max_seconds=21600.0,
            retry_max_attempts=2,
            **kwargs,
        )

    async def register_ticket(self, database: ReminderDatabase, channel_id: int = 10):
        return await database.register(channel_id, 20, "ticket-10", OPENED_AT)

    async def test_registers_todo_slot_once(self) -> None:
        reminders = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        self.assertTrue(await self.register_ticket(reminders))
        todo = self.make_database()

        self.assertTrue(await todo.register_todo(10))
        self.assertFalse(await todo.register_todo(10))

        due = await todo.claim_due_registrations()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["channel_id"], "10")
        self.assertEqual(due[0]["channel_name"], "ticket-10")
        self.assertEqual(due[0]["guild_id"], "20")

        await todo.mark_todo_registered("10", "todo-uuid-1")
        self.assertEqual(await todo.claim_due_registrations(), [])
        row = await todo.get_todo("10")
        self.assertEqual(row["todo_id"], "todo-uuid-1")
        self.assertEqual(row["status"], "registered")
        self.assertIs(row["decided"], False)

    async def test_failed_registration_backs_off_then_errors(self) -> None:
        reminders = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        await self.register_ticket(reminders)
        todo = self.make_database()
        await todo.register_todo(10)

        now = datetime.now(timezone.utc)
        self.assertEqual(len(await todo.claim_due_registrations(now)), 1)
        status, attempts = await todo.fail_todo_registration("10", "boom", now)
        self.assertEqual((status, attempts), ("pending", 1))
        self.assertEqual(await todo.claim_due_registrations(now), [])

        self.assertEqual(
            len(await todo.claim_due_registrations(now + timedelta(seconds=60))), 1
        )
        status, attempts = await todo.fail_todo_registration(
            "10", "boom again", now + timedelta(seconds=60)
        )
        self.assertEqual((status, attempts), ("error", 2))
        self.assertEqual(await todo.claim_due_registrations(), [])
        row = await todo.get_todo("10")
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["last_error"], "boom again")

    async def test_record_decision_counts_distinct_judges(self) -> None:
        reminders = ReminderDatabase(self.pool, reminder_after=timedelta(days=14))
        await self.register_ticket(reminders)
        todo = self.make_database()
        await todo.register_todo(10)
        await todo.mark_todo_registered("10", "todo-uuid-1")

        self.assertEqual(await todo.get_decision("10", 1), (None, 0))

        self.assertEqual(await todo.record_decision("10", 1, "approval"), 1)
        self.assertEqual(await todo.get_decision("10", 1), ("approval", 1))
        self.assertEqual(await todo.record_decision("10", 1, "reject"), 1)
        self.assertEqual(await todo.get_decision("10", 1), ("reject", 1))
        self.assertEqual(await todo.record_decision("10", 2, "approval"), 2)
        self.assertEqual(await todo.get_decision("10", 2), ("approval", 2))

        self.assertEqual(
            await todo.decision_tallies("10"),
            {"approval": 1, "reject": 1},
        )

        await todo.mark_todo_decided("10")
        row = await todo.get_todo("10")
        self.assertIs(row["decided"], True)


if __name__ == "__main__":
    unittest.main()
