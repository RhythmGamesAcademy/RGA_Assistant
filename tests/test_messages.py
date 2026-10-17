import unittest
from datetime import datetime, timedelta, timezone

from main import (
    build_failure_notification,
    build_operations_reminder_message,
    build_ticket_reminder_message,
    elapsed_days,
)

OPENED_AT = datetime(2026, 10, 1, 15, 0, tzinfo=timezone(timedelta(hours=9)))


class ElapsedDaysTests(unittest.TestCase):
    def test_counts_completed_days(self) -> None:
        now = OPENED_AT + timedelta(days=13, hours=23)
        self.assertEqual(elapsed_days(OPENED_AT, now), 13)

    def test_matches_full_interval(self) -> None:
        now = OPENED_AT.astimezone(timezone.utc) + timedelta(days=14)
        self.assertEqual(elapsed_days(OPENED_AT, now), 14)


class MessageBuilderTests(unittest.TestCase):
    def test_ticket_message_contains_link_and_jst_timestamp(self) -> None:
        message = build_ticket_reminder_message("ticket-10", OPENED_AT, 14, "20", "10")
        self.assertIn("チケット確認リマインダー", message)
        self.assertIn("https://discord.com/channels/20/10", message)
        self.assertIn("2026/10/01 15:00 JST", message)
        self.assertIn("14日", message)

    def test_operations_message_without_mention(self) -> None:
        message = build_operations_reminder_message("ticket-10", OPENED_AT, 14, "20", "10")
        self.assertTrue(message.startswith("**チケット確認リマインダー**"))
        self.assertIn("対応状況を確認してください。", message)
        self.assertIn("https://discord.com/channels/20/10", message)

    def test_operations_message_prepends_mention(self) -> None:
        message = build_operations_reminder_message(
            "ticket-10", OPENED_AT, 14, "20", "10", mention="<@&99>"
        )
        self.assertTrue(message.startswith("<@&99>\n"))

    def test_failure_notification_targets_retry_tool(self) -> None:
        message = build_failure_notification("20", "10", "ticket", 6, "boom")
        self.assertIn("https://discord.com/channels/20/10", message)
        self.assertIn("python -m tools.retry", message)
        self.assertIn("boom", message)


if __name__ == "__main__":
    unittest.main()
