import os
import tempfile
import unittest
from datetime import timedelta

from config import (
    load_env_files,
    parse_cancel_trigger,
    parse_category_ids,
    parse_discord_ids,
    parse_optional_id,
    parse_positive_float,
    parse_positive_int,
    parse_reminder_after,
    parse_reminder_targets,
    parse_trigger_texts,
    validate_mention_targets,
)


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


class ReminderTargetConfigTests(unittest.TestCase):
    def test_maps_target_names_to_delivery_targets(self) -> None:
        self.assertEqual(parse_reminder_targets("ticket"), ("ticket",))
        self.assertEqual(parse_reminder_targets("operations"), ("operations",))
        self.assertEqual(parse_reminder_targets("BOTH"), ("ticket", "operations"))

    def test_rejects_unknown_target(self) -> None:
        with self.assertRaises(ValueError):
            parse_reminder_targets("everywhere")


class MentionTargetConfigTests(unittest.TestCase):
    def test_accepts_role_only_user_only_or_neither(self) -> None:
        validate_mention_targets(123, None)
        validate_mention_targets(None, 456)
        validate_mention_targets(None, None)

    def test_rejects_setting_both_role_and_user(self) -> None:
        with self.assertRaises(ValueError):
            validate_mention_targets(123, 456)


class OptionalIdConfigTests(unittest.TestCase):
    def test_parses_present_and_absent_ids(self) -> None:
        self.assertEqual(parse_optional_id("123", "RGA_X"), 123)
        self.assertIsNone(parse_optional_id("", "RGA_X"))

    def test_rejects_invalid_ids(self) -> None:
        for value in ("abc", "0", "-1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_optional_id(value, "RGA_X")


class RetryConfigTests(unittest.TestCase):
    def test_parses_positive_values(self) -> None:
        self.assertEqual(parse_positive_float("60", "RGA_X"), 60.0)
        self.assertEqual(parse_positive_int("6", "RGA_X"), 6)

    def test_rejects_nonpositive_or_invalid_values(self) -> None:
        for value in ("0", "-5", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_positive_float(value, "RGA_X")
        for value in ("0", "-5", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_positive_int(value, "RGA_X")


class ReminderTriggerConfigTests(unittest.TestCase):
    def test_parses_comma_separated_keywords(self) -> None:
        self.assertEqual(
            parse_trigger_texts("終了, End"),
            ("終了", "End"),
        )

    def test_single_keyword_stays_a_one_tuple(self) -> None:
        self.assertEqual(parse_trigger_texts("End"), ("End",))

    def test_removes_duplicates_and_blank_entries(self) -> None:
        self.assertEqual(parse_trigger_texts("End, , End , 終了"), ("End", "終了"))

    def test_rejects_empty_value(self) -> None:
        for value in ("", "  ", ",", " , "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_trigger_texts(value)


class CancelTriggerConfigTests(unittest.TestCase):
    def test_accepts_known_trigger_names(self) -> None:
        self.assertEqual(parse_cancel_trigger("image"), "image")
        self.assertEqual(parse_cancel_trigger("keyword"), "keyword")

    def test_normalizes_case_and_whitespace(self) -> None:
        self.assertEqual(parse_cancel_trigger(" Image "), "image")
        self.assertEqual(parse_cancel_trigger("KEYWORD"), "keyword")

    def test_rejects_unknown_trigger_names(self) -> None:
        for value in ("", "text", "both", "image,keyword"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_cancel_trigger(value)


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


SPLIT_ENV_VARS = ("DISCORD_TOKEN", "RGA_REMINDER_VALUE")


class LoadEnvFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in SPLIT_ENV_VARS}
        for name in SPLIT_ENV_VARS:
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_setting_file_wins_over_env_file_for_duplicated_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            setting_path = os.path.join(directory, ".setting")
            env_path = os.path.join(directory, ".env")
            with open(setting_path, "w", encoding="utf-8") as file:
                file.write("RGA_REMINDER_VALUE=7\n")
            with open(env_path, "w", encoding="utf-8") as file:
                file.write("RGA_REMINDER_VALUE=14\nDISCORD_TOKEN=bot-token\n")

            load_env_files(setting_path, env_path)

            self.assertEqual(os.environ["RGA_REMINDER_VALUE"], "7")
            self.assertEqual(os.environ["DISCORD_TOKEN"], "bot-token")

    def test_process_environment_outranks_both_files(self) -> None:
        os.environ["RGA_REMINDER_VALUE"] = "3"
        with tempfile.TemporaryDirectory() as directory:
            setting_path = os.path.join(directory, ".setting")
            env_path = os.path.join(directory, ".env")
            with open(setting_path, "w", encoding="utf-8") as file:
                file.write("RGA_REMINDER_VALUE=7\n")
            with open(env_path, "w", encoding="utf-8") as file:
                file.write("RGA_REMINDER_VALUE=14\n")

            load_env_files(setting_path, env_path)

            self.assertEqual(os.environ["RGA_REMINDER_VALUE"], "3")

    def test_missing_files_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            load_env_files(
                os.path.join(directory, "absent-setting"),
                os.path.join(directory, "absent-env"),
            )

            self.assertEqual(os.environ.get("DISCORD_TOKEN"), None)
            self.assertEqual(os.environ.get("RGA_REMINDER_VALUE"), None)


if __name__ == "__main__":
    unittest.main()
