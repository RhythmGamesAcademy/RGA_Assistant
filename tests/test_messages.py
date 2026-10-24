import unittest
from datetime import datetime, timedelta, timezone

import discord

from application import build_application_status_field
from config import Config
from main import (
    build_delivery_message,
    build_failure_list_embed,
    build_failure_notification_embed,
    build_mention,
    build_operations_reminder_embed,
    build_ticket_reminder_embed,
    elapsed_days,
)

OPENED_AT = datetime(2026, 10, 1, 15, 0, tzinfo=timezone(timedelta(hours=9)))


def find_field(embed: discord.Embed, name: str):
    return next((f for f in embed.fields if f.name == name), None)


def make_config(**overrides) -> Config:
    settings = dict(
        discord_token="token",
        ticket_category_ids=(10,),
        excluded_channel_ids=(),
        cancel_trigger="image",
        reminder_trigger_texts=(),
        reminder_after=timedelta(days=14),
        reminder_targets=("ticket",),
        retention_days=90,
        operations_channel_id=555,
        operations_mention_role_id=None,
        operations_mention_user_id=None,
        retry_base_seconds=60.0,
        retry_max_seconds=21600.0,
        retry_max_attempts=6,
        database_url="postgresql://localhost/rga",
        lecture_role_ids=(),
        lecture_reminder_minutes=15,
        lecture_announce_channel_id=None,
        application_status_mode="simple",
        jev_api_key="",
        jev_model="typesafe/jev-1.14",
        application_status_confidence_threshold=0.7,
    )
    settings.update(overrides)
    return Config(**settings)


def make_delivery(**overrides) -> dict:
    delivery = {
        "channel_id": "10",
        "channel_name": "ticket-10",
        "guild_id": "20",
        "target": "ticket",
        "opened_at": OPENED_AT.astimezone(timezone.utc),
    }
    delivery.update(overrides)
    return delivery


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
        embed = build_ticket_reminder_embed(
            "ticket-10", OPENED_AT, 14, "20", "10", cancel_trigger="image"
        )
        text = embed_text(embed)
        self.assertEqual(embed.title, "チケット確認リマインダー")
        self.assertIn("https://discord.com/channels/20/10", text)
        self.assertIn("2026/10/01 15:00 JST", text)
        self.assertIn("14日", text)

    def test_image_mode_embed_explains_the_unread_image(self) -> None:
        embed = build_ticket_reminder_embed(
            "ticket-10", OPENED_AT, 14, "20", "10", cancel_trigger="image"
        )
        text = embed_text(embed)
        self.assertIn("申請画像が読み取れなかった", text)
        self.assertNotIn("審査をお願いします。", text)

    def test_keyword_mode_embed_keeps_the_review_request_wording(self) -> None:
        embed = build_ticket_reminder_embed(
            "ticket-10", OPENED_AT, 14, "20", "10", cancel_trigger="keyword"
        )
        self.assertIn("「審査をお願いします。」", embed_text(embed))

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


class DeliveryMessageTests(unittest.TestCase):
    def test_ticket_delivery_has_no_content_and_mentions_nobody(self) -> None:
        content, embed, allowed = build_delivery_message(make_delivery(), make_config())
        self.assertIsNone(content)
        self.assertEqual(embed.title, "チケット確認リマインダー")
        self.assertIn("https://discord.com/channels/20/10", embed_text(embed))
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)

    def test_operations_delivery_mentions_the_configured_role(self) -> None:
        config = make_config(
            reminder_targets=("ticket", "operations"),
            operations_mention_role_id=99,
        )
        content, embed, allowed = build_delivery_message(
            make_delivery(target="operations"), config
        )
        self.assertEqual(content, "<@&99>")
        self.assertIn("対応状況を確認してください。", embed_text(embed))
        self.assertEqual(allowed.roles, [discord.Object(id=99)])

    def test_operations_delivery_mentions_the_configured_user(self) -> None:
        config = make_config(
            reminder_targets=("ticket", "operations"),
            operations_mention_user_id=42,
        )
        content, embed, allowed = build_delivery_message(
            make_delivery(target="operations"), config
        )
        self.assertEqual(content, "<@42>")
        self.assertEqual(allowed.users, [discord.Object(id=42)])

    def test_operations_delivery_without_a_mention_target_is_silent(self) -> None:
        config = make_config(reminder_targets=("ticket", "operations"))
        content, embed, allowed = build_delivery_message(
            make_delivery(target="operations"), config
        )
        self.assertIsNone(content)
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.roles)
        self.assertFalse(allowed.users)

    def test_ticket_delivery_has_no_application_status_field(self) -> None:
        row = {"channel_id": "10", "has_application": True, "mode": "simple"}
        _, embed, _ = build_delivery_message(
            make_delivery(), make_config(), application_status=row
        )
        self.assertIsNone(find_field(embed, "申請の確認"))


class ApplicationStatusFieldTests(unittest.TestCase):
    def field_names_and_values(self, embed: discord.Embed) -> list[tuple[str, str]]:
        return [(f.name, f.value) for f in embed.fields]

    def test_operations_reminder_embed_adds_field_when_given(self) -> None:
        embed = build_operations_reminder_embed(
            "support-1",
            OPENED_AT,
            14,
            "20",
            "10",
            application_status_field="あり（添付画像検知）",
        )
        fields = self.field_names_and_values(embed)
        self.assertIn(("申請の確認", "あり（添付画像検知）"), fields)

    def test_operations_reminder_embed_has_no_field_when_absent(self) -> None:
        embed = build_operations_reminder_embed(
            "support-1", OPENED_AT, 14, "20", "10"
        )
        self.assertNotIn("申請の確認", [name for name, _ in self.field_names_and_values(embed)])

    def test_simple_row_shows_attachment_evidence_basis(self) -> None:
        # A stored row from simple criteria (e.g. on_message evidence or a
        # fallback) while jev is configured is labelled as the fallback.
        row = {"channel_id": "10", "has_application": True, "mode": "simple"}
        value = build_application_status_field(row, "jev", 0.7)
        self.assertEqual(value, "あり（simple・フォールバック）")

    def test_no_row_in_simple_mode_shows_no_evidence(self) -> None:
        value = build_application_status_field(None, "simple", 0.7)
        self.assertEqual(value, "なし（添付画像検知）")

    def test_confident_jev_row_shows_state_and_confidence(self) -> None:
        row = {
            "channel_id": "10",
            "has_application": False,
            "mode": "jev",
            "detail": {
                "application_state": "未申請",
                "confidence": 0.9,
            },
        }
        value = build_application_status_field(row, "jev", 0.7)
        self.assertEqual(value, "未申請（信頼 0.90 / Jev）")

    def test_low_confidence_jev_row_marks_human_review(self) -> None:
        row = {
            "channel_id": "10",
            "has_application": None,
            "mode": "jev",
            "detail": {
                "application_state": "申請中",
                "confidence": 0.4,
            },
        }
        value = build_application_status_field(row, "jev", 0.7)
        self.assertIn("参考: 申請中の可能性", value)
        self.assertIn("信頼 0.40 / Jev", value)
        self.assertIn("※ 人による確認を推奨", value)

    def test_operations_delivery_passes_application_status(self) -> None:
        row = {
            "channel_id": "10",
            "has_application": True,
            "mode": "simple",
            "detail": None,
        }
        config = make_config(reminder_targets=("ticket", "operations"))
        _, embed, _ = build_delivery_message(
            make_delivery(target="operations"), config, application_status=row
        )
        field = find_field(embed, "申請の確認")
        self.assertIsNotNone(field)
        self.assertEqual(field.value, "あり（添付画像検知）")

    def test_operations_delivery_without_status_shows_default(self) -> None:
        config = make_config(reminder_targets=("ticket", "operations"))
        _, embed, _ = build_delivery_message(
            make_delivery(target="operations"), config
        )
        field = find_field(embed, "申請の確認")
        self.assertIsNotNone(field)
        self.assertEqual(field.value, "なし（添付画像検知）")


if __name__ == "__main__":
    unittest.main()
