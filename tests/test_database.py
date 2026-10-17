import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import ReminderDatabase

OPENED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ReminderDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "reminders.sqlite3"
        self.database = ReminderDatabase(
            self.path,
            reminder_after=timedelta(days=14),
        )
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_registers_once_and_claims_when_due(self) -> None:
        self.assertTrue(self.database.register(10, 20, "ticket-10", OPENED_AT))
        self.assertFalse(self.database.register(10, 20, "ticket-10", OPENED_AT))

        self.assertEqual(
            self.database.claim_due(OPENED_AT + timedelta(days=13)),
            [],
        )
        due = self.database.claim_due(OPENED_AT + timedelta(days=14))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["channel_id"], "10")
        self.assertEqual(due[0]["target"], "ticket")
        self.assertTrue(self.database.is_sending("10", "ticket"))
        self.assertEqual(self.database.claim_due(OPENED_AT + timedelta(days=15)), [])
        self.database.cancel(10)
        self.assertFalse(self.database.is_sending("10", "ticket"))

    def test_cancelled_ticket_is_not_claimed(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        self.database.cancel(10)

        self.assertEqual(
            self.database.claim_due(OPENED_AT + timedelta(days=15)),
            [],
        )

    def test_registering_existing_cancelled_ticket_does_not_restore_reminder(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        self.database.cancel(10)
        self.database.register(10, 20, "ticket-10", OPENED_AT)

        self.assertEqual(
            self.database.claim_due(OPENED_AT + timedelta(days=15)),
            [],
        )

    def test_pending_reminder_is_restored_after_restart(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        self.database.claim_due(OPENED_AT + timedelta(days=14))
        restarted = ReminderDatabase(self.path, reminder_after=timedelta(days=14))
        restarted.initialize()

        self.assertEqual(
            len(restarted.claim_due(OPENED_AT + timedelta(days=14))),
            1,
        )

    def test_mark_sent_finalizes_reminder(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        self.database.claim_due(OPENED_AT + timedelta(days=14))
        self.database.mark_delivery_sent("10", "ticket")

        self.assertEqual(self.database.get_reminder("10")["status"], "sent")
        self.assertEqual(self.database.get_delivery("10", "ticket")["status"], "sent")

    def test_transient_failure_backs_off_then_retries(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        due = OPENED_AT + timedelta(days=14)
        self.database.claim_due(due)

        status, attempts = self.database.fail_delivery("10", "ticket", "boom", now=due)
        self.assertEqual((status, attempts), ("pending", 1))

        delivery = self.database.get_delivery("10", "ticket")
        self.assertEqual(
            delivery["next_attempt_at"],
            (due + timedelta(seconds=60)).isoformat(),
        )
        # Not due yet because of the backoff delay.
        self.assertEqual(self.database.claim_due(due + timedelta(seconds=59)), [])
        # Claimed again once the backoff elapses.
        self.assertEqual(
            len(self.database.claim_due(due + timedelta(seconds=60))),
            1,
        )

    def test_repeated_failures_end_in_error_state(self) -> None:
        database = ReminderDatabase(
            self.path,
            reminder_after=timedelta(days=14),
            retry_base_seconds=10,
            retry_max_seconds=10,
            retry_max_attempts=2,
        )
        database.initialize()
        database.register(10, 20, "ticket-10", OPENED_AT)
        now = OPENED_AT + timedelta(days=14)

        database.claim_due(now)
        self.assertEqual(
            database.fail_delivery("10", "ticket", "boom", now=now)[0], "pending"
        )
        now += timedelta(seconds=10)
        database.claim_due(now)
        self.assertEqual(
            database.fail_delivery("10", "ticket", "boom", now=now)[0], "error"
        )

        self.assertEqual(len(database.list_errors()), 1)
        self.assertEqual(database.retry_errors(10), 1)
        self.assertEqual(database.list_errors(), [])
        self.assertEqual(database.get_delivery("10", "ticket")["status"], "pending")

    def test_permanent_failure_goes_straight_to_error(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        due = OPENED_AT + timedelta(days=14)
        self.database.claim_due(due)

        status, attempts = self.database.fail_delivery(
            "10", "ticket", "missing permission", permanent=True, now=due
        )
        self.assertEqual((status, attempts), ("error", 1))


class MultiTargetReminderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "reminders.sqlite3"
        self.database = ReminderDatabase(
            self.path,
            reminder_after=timedelta(days=14),
            targets=("ticket", "operations"),
        )
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_creates_a_delivery_per_target(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        due = self.database.claim_due(OPENED_AT + timedelta(days=14))
        self.assertEqual({row["target"] for row in due}, {"ticket", "operations"})

    def test_partial_success_only_finalizes_after_both_sent(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        self.database.claim_due(OPENED_AT + timedelta(days=14))

        self.database.mark_delivery_sent("10", "ticket")
        self.assertEqual(self.database.get_reminder("10")["status"], "pending")

        self.database.mark_delivery_sent("10", "operations")
        self.assertEqual(self.database.get_reminder("10")["status"], "sent")

    def test_cancel_cancels_every_delivery(self) -> None:
        self.database.register(10, 20, "ticket-10", OPENED_AT)
        self.database.cancel(10)

        self.assertEqual(self.database.get_delivery("10", "ticket")["status"], "cancelled")
        self.assertEqual(
            self.database.get_delivery("10", "operations")["status"], "cancelled"
        )


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "legacy.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_legacy_database(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
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
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO ticket_reminders
                    (channel_id, guild_id, channel_name, opened_at, due_at,
                     status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ("10", "20", "ticket-10", OPENED_AT.isoformat(),
                     (OPENED_AT + timedelta(days=14)).isoformat(), "pending",
                     OPENED_AT.isoformat()),
                    ("11", "20", "ticket-11", OPENED_AT.isoformat(),
                     (OPENED_AT + timedelta(days=14)).isoformat(), "sent",
                     OPENED_AT.isoformat()),
                ],
            )

    def test_backfills_legacy_rows_with_ticket_delivery(self) -> None:
        self._create_legacy_database()

        database = ReminderDatabase(self.path, reminder_after=timedelta(days=14))
        database.initialize()

        self.assertEqual(database.get_delivery("10", "ticket")["status"], "pending")
        self.assertEqual(database.get_delivery("11", "ticket")["status"], "sent")
        self.assertEqual(
            len(database.claim_due(OPENED_AT + timedelta(days=14))),
            1,
        )

    def test_is_idempotent(self) -> None:
        self._create_legacy_database()

        database = ReminderDatabase(self.path, reminder_after=timedelta(days=14))
        database.initialize()
        database.initialize()

        self.assertEqual(database.get_delivery("10", "ticket")["status"], "pending")


if __name__ == "__main__":
    unittest.main()
