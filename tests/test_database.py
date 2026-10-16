import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import ReminderDatabase


class ReminderDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = ReminderDatabase(
            Path(self.temp_dir.name) / "reminders.sqlite3",
            reminder_after=timedelta(days=14),
        )
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_registers_once_and_claims_when_due(self) -> None:
        opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(self.database.register(10, 20, "ticket-10", opened_at))
        self.assertFalse(self.database.register(10, 20, "ticket-10", opened_at))

        self.assertEqual(
            self.database.claim_due(opened_at + timedelta(days=13)),
            [],
        )
        due = self.database.claim_due(opened_at + timedelta(days=14))
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["channel_id"], "10")
        self.assertTrue(self.database.is_sending("10"))
        self.assertEqual(self.database.claim_due(opened_at + timedelta(days=15)), [])
        self.database.cancel(10)
        self.assertFalse(self.database.is_sending("10"))

    def test_cancelled_ticket_is_not_claimed(self) -> None:
        opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.database.register(10, 20, "ticket-10", opened_at)
        self.database.cancel(10)

        self.assertEqual(
            self.database.claim_due(opened_at + timedelta(days=15)),
            [],
        )

    def test_registering_existing_cancelled_ticket_does_not_restore_reminder(self) -> None:
        opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.database.register(10, 20, "ticket-10", opened_at)
        self.database.cancel(10)
        self.database.register(10, 20, "ticket-10", opened_at)

        self.assertEqual(
            self.database.claim_due(opened_at + timedelta(days=15)),
            [],
        )

    def test_pending_reminder_is_restored_after_restart(self) -> None:
        opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.database.register(10, 20, "ticket-10", opened_at)
        self.database.claim_due(opened_at + timedelta(days=14))
        restarted = ReminderDatabase(
            self.database.path,
            reminder_after=timedelta(days=14),
        )
        restarted.initialize()

        self.assertEqual(
            len(restarted.claim_due(opened_at + timedelta(days=14))),
            1,
        )


if __name__ == "__main__":
    unittest.main()
