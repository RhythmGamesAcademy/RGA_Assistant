import unittest
from datetime import datetime, timedelta, timezone

import discord

from lecture import (
    build_announcement_embed,
    build_cancellation_embed,
    build_change_notice_embed,
    build_lecture_list_embed,
    build_reminder_embed,
    has_lecture_permission,
    is_past,
    parse_start_time,
    reminder_due_at,
)

JST = timezone(timedelta(hours=9))
STARTS_AT = datetime(2026, 10, 18, 15, 0, tzinfo=JST)


def embed_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    if embed.footer.text:
        parts.append(embed.footer.text)
    for field in embed.fields:
        parts += [field.name, field.value]
    return "\n".join(parts)


class ParseStartTimeTests(unittest.TestCase):
    def test_parses_jst_input_as_utc(self) -> None:
        self.assertEqual(
            parse_start_time("2026/10/18 15:00"),
            datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc),
        )

    def test_tolerates_surrounding_whitespace(self) -> None:
        self.assertEqual(
            parse_start_time(" 2026/10/18 15:00 "),
            datetime(2026, 10, 18, 6, 0, tzinfo=timezone.utc),
        )

    def test_rejects_invalid_input(self) -> None:
        for value in ("2026-10-18 15:00", "2026/10/18", "tomorrow", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_start_time(value)


class ReminderScheduleTests(unittest.TestCase):
    def test_due_at_subtracts_the_lead_time(self) -> None:
        self.assertEqual(
            reminder_due_at(STARTS_AT, 15),
            datetime(2026, 10, 18, 5, 45, tzinfo=timezone.utc),
        )

    def test_is_past(self) -> None:
        self.assertFalse(is_past(STARTS_AT, STARTS_AT - timedelta(seconds=1)))
        self.assertTrue(is_past(STARTS_AT, STARTS_AT))


class LecturePermissionTests(unittest.TestCase):
    def test_administrator_always_allowed(self) -> None:
        self.assertTrue(has_lecture_permission(True, set(), ()))
        self.assertTrue(has_lecture_permission(True, set(), (1, 2)))

    def test_configured_role_is_required(self) -> None:
        self.assertTrue(has_lecture_permission(False, {1, 2}, (2, 3)))
        self.assertFalse(has_lecture_permission(False, {1}, (2, 3)))

    def test_no_configured_roles_means_administrators_only(self) -> None:
        self.assertFalse(has_lecture_permission(False, {1, 2}, ()))


class LectureEmbedBuilderTests(unittest.TestCase):
    def test_announcement_contains_lecture_details(self) -> None:
        embed = build_announcement_embed(
            "基礎リズム論", "講師A", "説明文", STARTS_AT, 15
        )
        text = embed_text(embed)
        self.assertEqual(embed.title, "講義案内")
        self.assertIn("**基礎リズム論**", text)
        self.assertIn("講師A", text)
        self.assertIn("2026/10/18 15:00 JST", text)
        self.assertIn("説明文", text)
        self.assertIn("15分前", text)

    def test_announcement_omits_empty_description(self) -> None:
        embed = build_announcement_embed("基礎", "講師A", "", STARTS_AT, 15)
        self.assertNotIn("説明", [field.name for field in embed.fields])

    def test_reminder_mentions_the_lead_time(self) -> None:
        embed = build_reminder_embed("基礎リズム論", "講師A", STARTS_AT, 15)
        text = embed_text(embed)
        self.assertEqual(embed.title, "講義開始リマインダー")
        self.assertIn("15分後に開始します。", text)
        self.assertIn("2026/10/18 15:00 JST", text)

    def test_change_notice_and_cancellation(self) -> None:
        self.assertEqual(
            build_change_notice_embed("基礎", "講師A", STARTS_AT).title,
            "講義情報の変更",
        )
        cancellation = build_cancellation_embed("基礎", STARTS_AT)
        self.assertEqual(cancellation.title, "講義の中止")
        self.assertIn("2026/10/18 15:00 JST", embed_text(cancellation))

    def test_user_input_is_escaped(self) -> None:
        embed = build_announcement_embed("*強調*", "_講師_", "", STARTS_AT, 15)
        text = embed_text(embed)
        self.assertIn("\\*強調\\*", text)
        self.assertIn("\\_講師\\_", text)


class LectureListEmbedTests(unittest.TestCase):
    def _lecture(self, **overrides) -> dict:
        lecture = {
            "lecture_id": "abc1234567",
            "title": "基礎リズム論",
            "lecturer": "講師A",
            "starts_at": STARTS_AT.astimezone(timezone.utc).isoformat(),
            "status": "scheduled",
        }
        lecture.update(overrides)
        return lecture

    def test_empty_list(self) -> None:
        embed = build_lecture_list_embed([])
        self.assertEqual(embed.title, "講義予定一覧")
        self.assertEqual(embed.description, "登録されている講義予定はありません。")

    def test_lists_id_and_state(self) -> None:
        now = STARTS_AT - timedelta(days=1)
        text = embed_text(build_lecture_list_embed([self._lecture()], now=now))
        self.assertIn("`abc1234567`", text)
        self.assertIn("2026/10/18 15:00 JST", text)
        self.assertIn("[予定]", text)

        past = embed_text(
            build_lecture_list_embed([self._lecture()], now=STARTS_AT + timedelta(hours=1))
        )
        self.assertIn("[終了]", past)

        cancelled = embed_text(
            build_lecture_list_embed([self._lecture(status="cancelled")], now=now)
        )
        self.assertIn("[中止]", cancelled)


if __name__ == "__main__":
    unittest.main()
