import unittest
from datetime import datetime, timedelta, timezone

from meeting_log import (
    CHUNK_TARGET_CHARS,
    collectable_entries,
    compute_since,
    format_messages,
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


if __name__ == "__main__":
    unittest.main()
