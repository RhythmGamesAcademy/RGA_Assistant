import unittest
from datetime import datetime, timedelta, timezone

import discord

from main import (
    build_failure_list_embed,
    build_failure_notification_embed,
    build_mention,
    build_operations_reminder_embed,
    build_ticket_reminder_embed,
    elapsed_days,
)

OPENED_AT = datetime(2026, 10, 1, 15, 0, tzinfo=timezone(timedelta(hours=9)))


def embed_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    if embed.footer.text:
        parts.append(embed.footer.text)
    for field in embed.fields:
        parts += [field.name, field.value]
    return "\n".join(parts)


class ElapsedDaysTests(unittest.TestCase):
    def test_counts_completed_days(self) -> None:
        now = OPENED_AT + timedelta(days=13, hours=23)
        self.assertEqual(elapsed_days(OPENED_AT, now), 13)

    def test_matches_full_interval(self) -> None:
        now = OPENED_AT.astimezone(timezone.utc) + timedelta(days=14)
        self.assertEqual(elapsed_days(OPENED_AT, now), 14)


class MentionBuilderTests(unittest.TestCase):
    def test_role_mention(self) -> None:
        self.assertEqual(build_mention(99, None), "<@&99>")

    def test_user_mention(self) -> None:
        self.assertEqual(build_mention(None, 42), "<@42>")

    def test_role_takes_precedence_if_both_provided(self) -> None:
        self.assertEqual(build_mention(99, 42), "<@&99>")

    def test_no_mention_when_unset(self) -> None:
        self.assertIsNone(build_mention(None, None))


class TicketEmbedTests(unittest.TestCase):
    def test_ticket_embed_contains_link_and_jst_timestamp(self) -> None:
        embed = build_ticket_reminder_embed("ticket-10", OPENED_AT, 14, "20", "10")
        text = embed_text(embed)
        self.assertEqual(embed.title, "チケット確認リマインダー")
        self.assertIn("https://discord.com/channels/20/10", text)
        self.assertIn("2026/10/01 15:00 JST", text)
        self.assertIn("14日", text)

    def test_operations_embed_asks_to_review(self) -> None:
        embed = build_operations_reminder_embed("ticket-10", OPENED_AT, 14, "20", "10")
        text = embed_text(embed)
        self.assertEqual(embed.title, "チケット確認リマインダー")
        self.assertIn("対応状況を確認してください。", text)
        self.assertIn("https://discord.com/channels/20/10", text)

    def test_failure_embed_targets_retry_tool(self) -> None:
        embed = build_failure_notification_embed("20", "10", "ticket", 6, "boom")
        text = embed_text(embed)
        self.assertEqual(embed.title, "チケットリマインダー送信失敗")
        self.assertIn("https://discord.com/channels/20/10", text)
        self.assertIn("python -m tools.retry", text)
        self.assertIn("boom", text)

    def test_failure_list_embed_lists_errors(self) -> None:
        rows = [
            {
                "channel_id": "10",
                "target": "ticket",
                "attempts": 6,
                "last_error": "boom",
                "updated_at": OPENED_AT,
            }
        ]
        embed = build_failure_list_embed(rows)
        text = embed_text(embed)
        self.assertEqual(embed.title, "チケットリマインダー送信失敗一覧")
        self.assertIn("`10`", text)
        self.assertIn("boom", text)
        self.assertIn("/rga retry", text)

    def test_failure_list_embed_handles_empty_list(self) -> None:
        embed = build_failure_list_embed([])
        self.assertEqual(embed.description, "失敗状態の送信はありません。")


if __name__ == "__main__":
    unittest.main()
