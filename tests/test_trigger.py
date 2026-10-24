import unittest

from main import is_monitored_channel, is_reminder_cancel_message


class ReminderCancellationImageTests(unittest.TestCase):
    def test_cancels_on_image_attachment(self) -> None:
        self.assertTrue(
            is_reminder_cancel_message(("image/png",), "", 10, (10,), 100, (), "image", ())
        )

    def test_cancels_on_any_image_content_type(self) -> None:
        for content_type in ("image/jpeg", "image/gif", "image/webp", "image/apng"):
            with self.subTest(content_type=content_type):
                self.assertTrue(
                    is_reminder_cancel_message(
                        (content_type,), "", 10, (10,), 100, (), "image", ()
                    )
                )

    def test_cancels_when_an_image_is_among_other_attachments(self) -> None:
        self.assertTrue(
            is_reminder_cancel_message(
                ("text/plain", "image/png"), "", 10, (10,), 100, (), "image", ()
            )
        )

    def test_ignores_messages_without_image_attachments(self) -> None:
        for content_types in ((), (None,), ("text/plain",), (None, "application/pdf")):
            with self.subTest(content_types=content_types):
                self.assertFalse(
                    is_reminder_cancel_message(
                        content_types, "", 10, (10,), 100, (), "image", ()
                    )
                )

    def test_ignores_images_in_text_content_of_image_mode(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message(
                (), "画像を送りました", 10, (10,), 100, (), "image", ()
            )
        )

    def test_ignores_other_categories(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message(("image/png",), "", 11, (10,), 100, (), "image", ())
        )

    def test_matches_any_configured_category(self) -> None:
        self.assertTrue(
            is_reminder_cancel_message(
                ("image/png",), "", 20, (10, 20, 30), 100, (), "image", ()
            )
        )

    def test_ignores_excluded_channel_inside_configured_category(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message(
                ("image/png",), "", 10, (10,), 100, (100,), "image", ()
            )
        )

    def test_ignores_image_keyword_argument_in_image_mode(self) -> None:
        self.assertTrue(
            is_reminder_cancel_message(
                ("image/png",), "任意の本文", 10, (10,), 100, (), "image", ()
            )
        )


class ReminderCancellationKeywordTests(unittest.TestCase):
    def test_cancels_on_exact_match_of_any_keyword(self) -> None:
        triggers = ("審査をお願いします。", "終了")
        self.assertTrue(
            is_reminder_cancel_message(
                (), "審査をお願いします。", 10, (10,), 100, (), "keyword", triggers
            )
        )
        self.assertTrue(
            is_reminder_cancel_message((), "終了", 10, (10,), 100, (), "keyword", triggers)
        )

    def test_ignores_partial_and_case_variants(self) -> None:
        triggers = ("審査をお願いします。", "End")
        for content in ("end", "審査をお願いします", "審査をお願いします。 ", "please End"):
            with self.subTest(content=content):
                self.assertFalse(
                    is_reminder_cancel_message(
                        (), content, 10, (10,), 100, (), "keyword", triggers
                    )
                )

    def test_ignores_image_attachments_in_keyword_mode(self) -> None:
        self.assertFalse(
            is_reminder_cancel_message(
                ("image/png",), "本文なし", 10, (10,), 100, (), "keyword", ()
            )
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
