import json
import unittest
from datetime import datetime, timedelta, timezone

from embeds import MAX_DESCRIPTION
from meeting_log import (
    CHUNK_TARGET_CHARS,
    GeminiError,
    build_chunk_prompt,
    build_meeting_log_embed,
    build_merge_prompt,
    build_single_prompt,
    build_summary_text,
    collectable_entries,
    compute_since,
    format_messages,
    has_meeting_log_permission,
    is_empty_extraction,
    parse_gemini_response,
    split_into_chunks,
)


class ComputeSinceTests(unittest.TestCase):
    def test_recent_log_continues_from_covered_until(self) -> None:
        now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        latest = {
            "created_at": datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc),
            "covered_until": datetime(2026, 10, 27, 10, 30, tzinfo=timezone.utc),
        }
        self.assertEqual(compute_since(latest, now), latest["covered_until"])

    def test_old_log_falls_back_to_24h_window(self) -> None:
        now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        latest = {
            "created_at": datetime(2026, 10, 25, 11, 0, tzinfo=timezone.utc),
            "covered_until": datetime(2026, 10, 25, 10, 30, tzinfo=timezone.utc),
        }
        self.assertEqual(compute_since(latest, now), now - timedelta(hours=24))

    def test_no_log_uses_24h_window(self) -> None:
        now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_since(None, now), now - timedelta(hours=24))


class CollectTests(unittest.TestCase):
    def test_filters_bots_and_empty_and_sorts_oldest_first(self) -> None:
        entries = [
            (datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc), True, "Bot", "自動投稿"),
            (datetime(2026, 10, 27, 11, 0, tzinfo=timezone.utc), False, "太郎", "真面目な話"),
            (datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc), False, "花子", "先の話"),
            (datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc), False, "次郎", "   "),
        ]
        result = collectable_entries(entries)
        self.assertEqual([e[1] for e in result], ["花子", "太郎"])

    def test_format_uses_jst_and_truncates_body(self) -> None:
        entries = [
            (datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc), "太郎", "x" * 600),
        ]
        lines = format_messages(entries)
        self.assertEqual(lines, ["[2026/10/27 10:00] 太郎: " + "x" * 500])


class SplitTests(unittest.TestCase):
    def test_short_input_is_single_chunk(self) -> None:
        self.assertEqual(split_into_chunks(["a", "b"]), ["a\nb"])

    def test_split_only_at_message_boundaries(self) -> None:
        line = "y" * 600
        lines = [f"{line} #{i}" for i in range(50)]
        chunks = split_into_chunks(lines)
        self.assertTrue(all(len(c) >= CHUNK_TARGET_CHARS for c in chunks[:-1]))
        self.assertTrue(all(len(c) <= CHUNK_TARGET_CHARS + 500 + 100 for c in chunks))
        for chunk in chunks:
            for original in lines:
                if original in chunk:
                    self.assertIn(original, chunk)
        self.assertEqual(chunks[0].split("\n")[0], lines[0])

    def test_one_line_never_split_even_if_longer_than_target(self) -> None:
        lines = ["z" * (CHUNK_TARGET_CHARS + 10)]
        self.assertEqual(split_into_chunks(lines), lines)


class PermissionTests(unittest.TestCase):
    def test_administrator_is_always_allowed(self) -> None:
        self.assertTrue(has_meeting_log_permission(True, set(), ()))

    def test_empty_role_ids_allow_nobody_else(self) -> None:
        self.assertFalse(has_meeting_log_permission(False, {10}, ()))

    def test_role_intersection_is_required(self) -> None:
        self.assertTrue(has_meeting_log_permission(False, {10, 20}, (20,)))


class ParseTests(unittest.TestCase):
    def test_parses_full_merge_payload(self) -> None:
        data = {
            "similar": True,
            "topics": [{"title": "申請フロー", "summary": "…"}],
            "decisions": ["Aを採用"],
            "todos": ["Bさんが確認"],
        }
        result = parse_gemini_response(data, require_similar=True)
        self.assertTrue(result["similar"])
        self.assertEqual(result["topics"][0]["title"], "申請フロー")
        self.assertEqual(result["decisions"], ["Aを採用"])

    def test_missing_similar_is_error_when_required(self) -> None:
        with self.assertRaises(GeminiError):
            parse_gemini_response({"topics": [], "decisions": [], "todos": []}, require_similar=True)

    def test_malformed_types_raise(self) -> None:
        with self.assertRaises(GeminiError):
            parse_gemini_response({"similar": False, "topics": "nope", "decisions": [], "todos": []}, require_similar=False)
        with self.assertRaises(GeminiError):
            parse_gemini_response("not a dict", require_similar=False)

    def test_empty_extraction_detection(self) -> None:
        self.assertTrue(is_empty_extraction({"similar": False, "topics": [], "decisions": [], "todos": []}))
        self.assertFalse(is_empty_extraction({"similar": False, "topics": [{"title": "t", "summary": "s"}], "decisions": [], "todos": []}))


class PromptTests(unittest.TestCase):
    def test_single_prompt_includes_previous_summary_when_given(self) -> None:
        prompt = build_single_prompt("会話本文", "前回の議事録")
        self.assertIn("会話本文", prompt)
        self.assertIn("前回の議事録", prompt)
        self.assertIn("おふざけ", prompt)

    def test_single_prompt_without_previous_mentions_judgement(self) -> None:
        prompt = build_single_prompt("会話本文", None)
        self.assertIn("会話本文", prompt)

    def test_merge_prompt_carries_chunk_results(self) -> None:
        prompt = build_merge_prompt(None, [{"topics": [], "decisions": ["X"], "todos": []}])
        self.assertIn("X", prompt)
        self.assertIn("500", prompt)


class EmbedTests(unittest.TestCase):
    def test_embed_contains_topics_decisions_and_window(self) -> None:
        result = {
            "similar": False,
            "topics": [{"title": "申請フロー", "summary": "見直しの話"}],
            "decisions": ["Aを採用"],
            "todos": ["Bさんが確認"],
        }
        start = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
        end = datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc)
        embed = build_meeting_log_embed(result, start, end, None, None, None)
        self.assertIn("2026/10/27 18:00", embed.description or "")
        self.assertIn("申請フロー", embed.description)
        self.assertIn("Aを採用", embed.description)

    def test_similar_result_links_previous_message(self) -> None:
        result = {"similar": True, "topics": [{"title": "t", "summary": "s"}], "decisions": [], "todos": []}
        embed = build_meeting_log_embed(
            result,
            datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc),
            100,
            200,
            previous_message_id=999,
        )
        self.assertIn("https://discord.com/channels/100/200/999", embed.description or "")

    def test_description_never_exceeds_limit(self) -> None:
        result = {
            "similar": False,
            "topics": [{"title": "t" * 50, "summary": "s" * 2000} for _ in range(20)],
            "decisions": ["d" * 500] * 30,
            "todos": ["w" * 500] * 30,
        }
        embed = build_meeting_log_embed(
            result,
            datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 27, 10, 0, tzinfo=timezone.utc),
            None,
            None,
            None,
        )
        self.assertLessEqual(len(embed.description or ""), MAX_DESCRIPTION)

    def test_summary_text_round_trips(self) -> None:
        result = {"similar": True, "topics": [{"title": "t", "summary": "s"}], "decisions": ["d"], "todos": []}
        text = build_summary_text(result)
        self.assertEqual(json.loads(text), result)


if __name__ == "__main__":
    unittest.main()
