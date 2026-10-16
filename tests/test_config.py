import unittest
from datetime import timedelta

from config import parse_category_ids, parse_discord_ids, parse_reminder_after


class ReminderIntervalConfigTests(unittest.TestCase):
    def test_supports_multiple_time_units(self) -> None:
        self.assertEqual(parse_reminder_after(30, "minutes"), timedelta(minutes=30))
        self.assertEqual(parse_reminder_after(2, "hours"), timedelta(hours=2))
        self.assertEqual(parse_reminder_after(2, "weeks"), timedelta(weeks=2))

    def test_unit_is_case_insensitive_and_defaults_to_days_elsewhere(self) -> None:
        self.assertEqual(parse_reminder_after(14, "DAYS"), timedelta(days=14))

    def test_rejects_unknown_units_and_nonpositive_intervals(self) -> None:
        with self.assertRaises(ValueError):
            parse_reminder_after(1, "months")
        with self.assertRaises(ValueError):
            parse_reminder_after(0, "days")


class CategoryIdConfigTests(unittest.TestCase):
    def test_parses_multiple_category_ids(self) -> None:
        self.assertEqual(parse_category_ids("10, 20,30"), (10, 20, 30))

    def test_removes_duplicate_category_ids(self) -> None:
        self.assertEqual(parse_category_ids("10,20,10"), (10, 20))

    def test_rejects_invalid_category_ids(self) -> None:
        for value in ("", "10,nope", "0", "-2"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_category_ids(value)


class ExcludedChannelConfigTests(unittest.TestCase):
    def test_parses_optional_excluded_channel_ids(self) -> None:
        self.assertEqual(
            parse_discord_ids("100, 200", "RGA_EXCLUDED_CHANNEL_IDS", allow_empty=True),
            (100, 200),
        )
        self.assertEqual(
            parse_discord_ids("", "RGA_EXCLUDED_CHANNEL_IDS", allow_empty=True),
            (),
        )

    def test_rejects_invalid_excluded_channel_ids(self) -> None:
        with self.assertRaises(ValueError):
            parse_discord_ids("100,nope", "RGA_EXCLUDED_CHANNEL_IDS", allow_empty=True)


if __name__ == "__main__":
    unittest.main()
