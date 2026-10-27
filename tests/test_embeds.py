import unittest

import discord

from embeds import (
    COLOR_ERROR,
    COLOR_SUCCESS,
    MAX_DESCRIPTION,
    error_embed,
    success_embed,
    truncate,
)
from main import build_help_embed


def embed_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    if embed.footer.text:
        parts.append(embed.footer.text)
    for field in embed.fields:
        parts += [field.name, field.value]
    return "\n".join(parts)


class TruncateTests(unittest.TestCase):
    def test_keeps_short_values(self) -> None:
        self.assertEqual(truncate("短文", 10), "短文")

    def test_truncates_with_ellipsis(self) -> None:
        value = truncate("a" * 20, 10)
        self.assertEqual(len(value), 10)
        self.assertTrue(value.endswith("…"))


class EmbedHelperTests(unittest.TestCase):
    def test_error_embed(self) -> None:
        embed = error_embed("問題が発生しました")
        self.assertEqual(embed.title, "エラー")
        self.assertEqual(embed.description, "問題が発生しました")
        self.assertEqual(embed.color, COLOR_ERROR)

    def test_error_embed_truncates_long_descriptions(self) -> None:
        embed = error_embed("x" * (MAX_DESCRIPTION + 100))
        self.assertLessEqual(len(embed.description), MAX_DESCRIPTION)

    def test_success_embed(self) -> None:
        embed = success_embed("完了", "登録しました")
        self.assertEqual(embed.title, "完了")
        self.assertEqual(embed.description, "登録しました")
        self.assertEqual(embed.color, COLOR_SUCCESS)


class HelpEmbedTests(unittest.TestCase):
    def test_help_lists_ticket_and_lecture_features(self) -> None:
        text = embed_text(build_help_embed("image"))
        self.assertIn("チケットリマインダー", text)
        self.assertIn("/lecture create", text)
        self.assertIn("/lecture edit", text)
        self.assertIn("/lecture cancel", text)
        self.assertIn("/lecture list", text)
        self.assertIn("/rga help", text)

    def test_image_mode_help_describes_image_cancellation(self) -> None:
        text = embed_text(build_help_embed("image"))
        self.assertIn("画像が添付された投稿があると", text)
        self.assertNotIn("キーワード", text)

    def test_keyword_mode_help_describes_keyword_cancellation(self) -> None:
        text = embed_text(build_help_embed("keyword"))
        self.assertIn("キャンセル用のキーワードに完全一致する投稿があると", text)
        self.assertNotIn("画像が添付された投稿があると", text)


if __name__ == "__main__":
    unittest.main()
