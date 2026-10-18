import unittest

from main import is_reminder_cancel_message


class ReminderCancelKeywordTests(unittest.TestCase):
    def test_matches_any_keyword_in_the_tuple(self) -> None:
        triggers = ("終了", "End")
        self.assertTrue(is_reminder_cancel_message("End", 10, (10,), 100, (), triggers))
        self.assertTrue(
            is_reminder_cancel_message("終了", 10, (10,), 100, (), triggers)
        )

    def test_ignores_messages_not_matching_any_keyword(self) -> None:
        triggers = ("終了", "End")
        self.assertFalse(
            is_reminder_cancel_message("end", 10, (10,), 100, (), triggers)
        )
        self.assertFalse(
            is_reminder_cancel_message("終わりました", 10, (10,), 100, (), triggers)
        )


if __name__ == "__main__":
    unittest.main()
