import unittest

from main import is_monitored_channel, is_reminder_cancel_message


class ReminderCancellationMessageTests(unittest.TestCase):
    def test_matches_exact_message_in_configured_category(self) -> None:
        self.assertTrue(
            is_reminder_cancel_message("help", 10, (10,), 100, (), ("help",))
        )

    def test_ignores_other_categories(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message("Need help", 11, (10,), 100, (), ("help",))
        )

    def test_matches_any_configured_category(self) -> None:
        self.assertTrue(
            is_reminder_cancel_message("help", 20, (10, 20, 30), 100, (), ("help",))
        )

    def test_ignores_categories_not_in_configured_list(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message("help", 40, (10, 20, 30), 100, (), ("help",))
        )

    def test_ignores_excluded_channel_inside_configured_category(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message("help", 10, (10,), 100, (100,), ("help",))
        )

    def test_ignores_messages_that_only_contain_trigger(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message("please help", 10, (10,), 100, (), ("help",))
        )

    def test_exact_match_is_case_sensitive(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message("Help", 10, (10,), 100, (), ("help",))
        )


class MonitoredChannelTests(unittest.TestCase):
    def test_monitors_text_channel_in_any_configured_category(self) -> None:
        self.assertTrue(is_monitored_channel(20, 100, (10, 20), ()))

    def test_ignores_channel_outside_configured_categories(self) -> None:
        self.assertFalse(is_monitored_channel(30, 100, (10, 20), ()))

    def test_ignores_excluded_channel(self) -> None:
        self.assertFalse(is_monitored_channel(20, 100, (10, 20), (100,)))

    def test_ignores_channel_without_category(self) -> None:
        self.assertFalse(is_monitored_channel(None, 100, (10, 20), ()))


if __name__ == "__main__":
    unittest.main()
