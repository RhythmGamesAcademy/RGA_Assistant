import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import LectureDatabase

JST = timezone(timedelta(hours=9))
STARTS_AT = datetime(2026, 10, 18, 15, 0, tzinfo=JST)
DUE_AT = STARTS_AT.astimezone(timezone.utc) - timedelta(minutes=15)


class LectureDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "lectures.sqlite3"
        self.database = LectureDatabase(self.path)
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create(self, starts_at: datetime = STARTS_AT, lead_minutes: int = 15) -> dict:
        return self.database.create(
            20,
            title="基礎リズム論",
            lecturer="講師A",
            description="説明",
            starts_at=starts_at,
            announce_channel_id=30,
            reminder_lead_minutes=lead_minutes,
            created_by=40,
        )

    def test_create_stores_utc_and_computes_due_at(self) -> None:
        lecture = self._create()

        self.assertEqual(len(lecture["lecture_id"]), LectureDatabase.ID_LENGTH)
        self.assertEqual(lecture["status"], "scheduled")
        self.assertEqual(lecture["reminder_status"], "pending")
        self.assertEqual(lecture["starts_at"], STARTS_AT.astimezone(timezone.utc).isoformat())
        self.assertEqual(lecture["reminder_due_at"], DUE_AT.isoformat())

    def test_create_allocates_unique_ids(self) -> None:
        ids = {self._create()["lecture_id"] for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_claim_due_only_after_the_lead_time(self) -> None:
        lecture = self._create()

        self.assertEqual(self.database.claim_due(DUE_AT - timedelta(seconds=1)), [])
        claimed = self.database.claim_due(DUE_AT)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["lecture_id"], lecture["lecture_id"])
        self.assertEqual(
            self.database.get(lecture["lecture_id"])["reminder_status"], "sending"
        )
        # Already claimed, so it is not returned twice.
        self.assertEqual(self.database.claim_due(DUE_AT + timedelta(minutes=1)), [])

    def test_reminder_is_expired_once_the_lecture_started(self) -> None:
        lecture = self._create()

        self.assertEqual(self.database.claim_due(STARTS_AT), [])
        self.assertEqual(
            self.database.get(lecture["lecture_id"])["reminder_status"], "expired"
        )

    def test_lecture_registered_shortly_before_start_still_reminds(self) -> None:
        starts_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        lecture = self._create(starts_at=starts_at, lead_minutes=15)

        claimed = self.database.claim_due()
        self.assertEqual([row["lecture_id"] for row in claimed], [lecture["lecture_id"]])

    def test_transient_failure_is_retried_after_the_delay(self) -> None:
        lecture = self._create()
        self.database.claim_due(DUE_AT)

        self.database.release_reminder(
            lecture["lecture_id"], "boom", retry_seconds=60, now=DUE_AT
        )
        self.assertEqual(
            self.database.get(lecture["lecture_id"])["next_attempt_at"],
            (DUE_AT + timedelta(seconds=60)).isoformat(),
        )
        self.assertEqual(self.database.claim_due(DUE_AT + timedelta(seconds=59)), [])
        self.assertEqual(len(self.database.claim_due(DUE_AT + timedelta(seconds=60))), 1)

    def test_permanent_failure_expires_the_reminder(self) -> None:
        lecture = self._create()
        self.database.claim_due(DUE_AT)

        self.database.expire_reminder(lecture["lecture_id"], "missing permission")
        record = self.database.get(lecture["lecture_id"])
        self.assertEqual(record["reminder_status"], "expired")
        self.assertEqual(record["last_error"], "missing permission")

    def test_mark_reminder_sent_stores_the_message(self) -> None:
        lecture = self._create()
        self.database.claim_due(DUE_AT)

        self.database.mark_reminder_sent(lecture["lecture_id"], 555)
        record = self.database.get(lecture["lecture_id"])
        self.assertEqual(record["reminder_status"], "sent")
        self.assertEqual(record["reminder_message_id"], "555")
        self.assertIsNotNone(record["reminder_sent_at"])

    def test_cancel_cancels_the_pending_reminder(self) -> None:
        lecture = self._create()

        cancelled = self.database.cancel(lecture["lecture_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["reminder_status"], "cancelled")
        self.assertEqual(self.database.claim_due(DUE_AT), [])

    def test_cancel_after_sent_keeps_the_sent_state(self) -> None:
        lecture = self._create()
        self.database.claim_due(DUE_AT)
        self.database.mark_reminder_sent(lecture["lecture_id"], 555)

        cancelled = self.database.cancel(lecture["lecture_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["reminder_status"], "sent")

    def test_update_recomputes_the_due_at_only_with_a_new_start(self) -> None:
        lecture = self._create()

        renamed = self.database.update(lecture["lecture_id"], title="応用リズム論")
        self.assertEqual(renamed["title"], "応用リズム論")
        self.assertEqual(renamed["reminder_due_at"], DUE_AT.isoformat())

        moved_start = STARTS_AT + timedelta(days=1)
        moved = self.database.update(lecture["lecture_id"], starts_at=moved_start)
        self.assertEqual(
            moved["reminder_due_at"],
            (moved_start.astimezone(timezone.utc) - timedelta(minutes=15)).isoformat(),
        )

    def test_update_after_sent_does_not_reset_the_reminder(self) -> None:
        lecture = self._create()
        self.database.claim_due(DUE_AT)
        self.database.mark_reminder_sent(lecture["lecture_id"], 555)

        updated = self.database.update(lecture["lecture_id"], title="変更後")
        self.assertEqual(updated["reminder_status"], "sent")
        self.assertEqual(self.database.claim_due(DUE_AT + timedelta(days=1)), [])

    def test_delete_removes_the_lecture(self) -> None:
        lecture = self._create()

        self.assertTrue(self.database.delete(lecture["lecture_id"]))
        self.assertIsNone(self.database.get(lecture["lecture_id"]))
        self.assertFalse(self.database.delete(lecture["lecture_id"]))

    def test_list_orders_by_start_and_hides_cancelled(self) -> None:
        later = self._create(starts_at=STARTS_AT + timedelta(days=1))
        sooner = self._create()
        cancelled = self._create(starts_at=STARTS_AT + timedelta(days=2))
        self.database.cancel(cancelled["lecture_id"])

        listed = self.database.list_lectures(20)
        self.assertEqual([row["lecture_id"] for row in listed], [sooner["lecture_id"], later["lecture_id"]])

        with_cancelled = self.database.list_lectures(20, include_cancelled=True)
        self.assertEqual(len(with_cancelled), 3)

    def test_sending_reminder_is_restored_after_restart(self) -> None:
        lecture = self._create()
        self.database.claim_due(DUE_AT)

        restarted = LectureDatabase(self.path)
        restarted.initialize()

        self.assertEqual(
            restarted.get(lecture["lecture_id"])["reminder_status"], "pending"
        )
        self.assertEqual(len(restarted.claim_due(DUE_AT)), 1)


if __name__ == "__main__":
    unittest.main()
