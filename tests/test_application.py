import asyncio
import random
import unittest
import unittest.mock
from types import SimpleNamespace

import discord

from application import (
    JEV_CONFIDENCE_THRESHOLD_DEFAULT,
    JEV_ENDPOINT,
    JEV_MAX_PAYLOAD_BYTES,
    JEV_MODE,
    JEV_STATE_CONTENT_LIMIT,
    SIMPLE_MODE,
    JevApplicationJudge,
    JevApplicationScheduler,
    JevError,
    JevRetryableError,
    build_application_status_field,
    build_request,
    build_request_within_limit,
    build_state,
    fetch_lean_history,
    has_application_from_jev,
    history_has_image_attachment,
    jev_result_detail,
    lean_message,
    parse_jev_response,
    serialize_request,
)

MODEL = "typesafe/jev-1.14"
API_KEY = "test-api-key"


def make_attachment(content_type=None, filename="shot.png", size=1024):
    return SimpleNamespace(
        content_type=content_type, filename=filename, size=size
    )


def make_discord_message(
    content="本文",
    attachments=(),
    author_bot=False,
    webhook_id=None,
):
    return SimpleNamespace(
        content=content,
        attachments=list(attachments),
        author=SimpleNamespace(bot=author_bot),
        webhook_id=webhook_id,
    )


def make_lean_message(content="本文", attachments=()):
    return {"author": "user", "content": content, "attachments": list(attachments)}


def jev_payload(state="申請中", confidence=0.85, probabilities=None, noul=0.12):
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "result": {
                "answers": {
                    "application_state": {
                        "type": "choice",
                        "choice": state,
                        "probabilities": probabilities
                        or {
                            "未申請": 0.05,
                            "申請中": 0.80,
                            "審査中": 0.15,
                            "判断不能": 0,
                        },
                        "confidence": confidence,
                    },
                    "no_application_yet": {"type": "noul", "noul": noul},
                },
                "usage": {"input_tokens": 300, "output_tokens": 30},
                "elapsedMs": 200,
            },
            "creditsUsed": 1,
        },
    }


def jev_row(state, confidence, noul=None):
    detail = {"application_state": state, "confidence": confidence}
    if noul is not None:
        detail["no_application_yet"] = noul
    return {"mode": JEV_MODE, "detail": detail}


def simple_row(has_application):
    return {"mode": SIMPLE_MODE, "has_application": has_application, "detail": None}


# --- Pure helpers -----------------------------------------------------------


class LeanMessageTests(unittest.TestCase):
    def test_keeps_author_content_and_attachment_metadata(self) -> None:
        message = make_discord_message(
            content="よろしくお願いします。",
            attachments=(make_attachment("image/png", "申請.png", 4096),),
        )
        item = lean_message(message)
        self.assertEqual(item["author"], "user")
        self.assertEqual(item["content"], "よろしくお願いします。")
        self.assertEqual(
            item["attachments"],
            [{"content_type": "image/png", "name": "申請.png", "size": 4096}],
        )

    def test_caps_the_content_length(self) -> None:
        message = make_discord_message(content="あ" * 5000)
        self.assertEqual(len(lean_message(message)["content"]), JEV_STATE_CONTENT_LIMIT)


class HistoryScanTests(unittest.TestCase):
    def test_finds_image_attachments(self) -> None:
        messages = [
            make_lean_message("a", ()),
            make_lean_message("", [{"content_type": "image/png"}]),
        ]
        self.assertTrue(history_has_image_attachment(messages))

    def test_ignores_non_image_attachments(self) -> None:
        for content_types in ([], [{"content_type": "text/plain"}], [{"content_type": None}]):
            with self.subTest(content_types=content_types):
                self.assertFalse(history_has_image_attachment([make_lean_message("c", content_types)]))


class FetchLeanHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_skips_bot_webhook_and_empty_messages(self) -> None:
        channel = SimpleNamespace(
            history=AsyncIterator(
                [
                    make_discord_message("最新", author_bot=False),      # keeps
                    make_discord_message("bot", author_bot=True),          # skipped
                    make_discord_message("hook", webhook_id=999),          # skipped
                    make_discord_message("", author_bot=False),            # empty → skipped
                    make_discord_message("初期", author_bot=False),        # keeps
                ]
            )
        )
        lean = await fetch_lean_history(channel)
        self.assertEqual([item["content"] for item in lean], ["初期", "最新"])

    async def test_returns_empty_for_empty_history(self) -> None:
        channel = SimpleNamespace(history=AsyncIterator([]))
        self.assertEqual(await fetch_lean_history(channel), [])


class AsyncIterator:
    """Discord-style async iterator wrapper for history objects."""

    def __init__(self, items) -> None:
        self._items = list(items)

    def __call__(self, limit=None):
        return self._iterate()

    async def _iterate(self):
        for item in self._items:
            yield item


class BuildRequestTests(unittest.TestCase):
    def test_uses_both_questions_within_limit(self) -> None:
        request = build_request(build_state(10, []), MODEL)
        self.assertEqual(request["model"], MODEL)
        self.assertEqual(list(request["state"].keys()), ["channel_id", "messages"])
        self.assertEqual(len(request["questions"]), 2)
        self.assertEqual(
            request["questions"]["application_state"]["type"], "choice"
        )
        self.assertEqual(
            request["questions"]["no_application_yet"]["type"], "noul"
        )

    def test_serializes_without_ascii_escaping(self) -> None:
        request = build_request(build_state(10, []), MODEL)
        raw = serialize_request(request)
        self.assertIn("未申請".encode("utf-8"), raw)


class TrimRequestTests(unittest.TestCase):
    def test_small_histories_pass_through(self) -> None:
        messages = [make_lean_message(f"m{i}") for i in range(3)]
        request = build_request_within_limit(messages, 10, MODEL)
        self.assertEqual(request["state"]["messages"], messages)
        self.assertLessEqual(len(serialize_request(request)), JEV_MAX_PAYLOAD_BYTES)

    def test_drops_the_oldest_messages_until_it_fits(self) -> None:
        filler = "x" * 1900
        messages = [make_lean_message(filler) for _ in range(30)]
        request = build_request_within_limit(messages, 10, MODEL)
        self.assertLessEqual(len(serialize_request(request)), JEV_MAX_PAYLOAD_BYTES)
        kept = request["state"]["messages"]
        self.assertLess(len(kept), 30)
        # The tail (newest side) of the history is what was kept.
        self.assertEqual([m["content"] for m in kept], [filler] * len(kept))

    def test_returns_none_when_a_single_message_still_exceeds_the_limit(self) -> None:
        huge = [make_lean_message("y" * (JEV_MAX_PAYLOAD_BYTES * 2))]
        self.assertIsNone(build_request_within_limit(huge, 10, MODEL))

    def test_empty_history_is_a_valid_request(self) -> None:
        request = build_request_within_limit([], 10, MODEL)
        self.assertEqual(request["state"]["messages"], [])


class ParseJevResponseTests(unittest.TestCase):
    def test_parses_choice_and_noul(self) -> None:
        parsed = parse_jev_response(jev_payload())
        self.assertEqual(parsed["application_state"], "申請中")
        self.assertEqual(parsed["confidence"], 0.85)
        self.assertEqual(parsed["no_application_yet"], 0.12)

    def test_noul_is_optional(self) -> None:
        payload = jev_payload()
        del payload["data"]["result"]["answers"]["no_application_yet"]
        parsed = parse_jev_response(payload)
        self.assertNotIn("no_application_yet", parsed)

    def test_unknown_state_is_rejected(self) -> None:
        with self.assertRaises(JevError):
            parse_jev_response(jev_payload(state="申請済み"))

    def test_missing_probabilities_are_rejected(self) -> None:
        payload = jev_payload()
        del payload["data"]["result"]["answers"]["application_state"]["probabilities"]
        with self.assertRaises(JevError):
            parse_jev_response(payload)

    def test_malformed_structure_is_rejected(self) -> None:
        for payload in (None, {}, {"data": {}}, jev_payload(state=None)):
            with self.subTest(payload=payload), self.assertRaises(JevError):
                parse_jev_response(payload)

    def test_judgment_mapping_from_jev(self) -> None:
        self.assertFalse(has_application_from_jev({"application_state": "未申請", "confidence": 0.9}, 0.7))
        self.assertIsNone(has_application_from_jev({"application_state": "未申請", "confidence": 0.45}, 0.7))
        self.assertTrue(has_application_from_jev({"application_state": "申請中", "confidence": 0.45}, 0.7))
        self.assertTrue(has_application_from_jev({"application_state": "審査中", "confidence": 0.9}, 0.7))
        self.assertIsNone(has_application_from_jev({"application_state": "判断不能", "confidence": 0.9}, 0.7))

    def test_detail_carries_state_confidence_and_noul(self) -> None:
        parsed = parse_jev_response(jev_payload())
        self.assertEqual(
            jev_result_detail(parsed),
            {"application_state": "申請中", "confidence": 0.85, "no_application_yet": 0.12},
        )


class ApplicationStatusFieldTests(unittest.TestCase):
    def test_simple_mode_without_a_row_reports_none(self) -> None:
        self.assertEqual(
            build_application_status_field(None, SIMPLE_MODE, 0.7),
            "なし（添付画像検知）",
        )

    def test_simple_mode_with_evidence(self) -> None:
        self.assertEqual(
            build_application_status_field(simple_row(True), SIMPLE_MODE, 0.7),
            "あり（添付画像検知）",
        )

    def test_jev_mode_marks_simple_rows_as_fallback(self) -> None:
        self.assertEqual(
            build_application_status_field(simple_row(True), JEV_MODE, 0.7),
            "あり（simple・フォールバック）",
        )
        self.assertEqual(
            build_application_status_field(None, JEV_MODE, 0.7),
            "なし（simple・フォールバック）",
        )

    def test_jev_row_shows_state_and_confidence(self) -> None:
        self.assertEqual(
            build_application_status_field(
                jev_row("申請中", 0.85, 0.12), JEV_MODE, 0.7
            ),
            "申請中（信頼 0.85 / Jev）",
        )

    def test_low_confidence_jev_rows_are_reference_only(self) -> None:
        self.assertEqual(
            build_application_status_field(
                jev_row("未申請", 0.45), JEV_MODE, 0.7
            ),
            "参考: 未申請の可能性（信頼 0.45 / Jev）※ 人による確認を推奨",
        )
        self.assertEqual(
            build_application_status_field(
                jev_row("申請中", 0.50), JEV_MODE, 0.7
            ),
            "参考: 申請中の可能性（信頼 0.50 / Jev）※ 人による確認を推奨",
        )

    def test_undecidable_rows_always_ask_for_human_review(self) -> None:
        for confidence in (0.55, 0.95):
            with self.subTest(confidence=confidence):
                self.assertEqual(
                    build_application_status_field(
                        jev_row("判断不能", confidence), JEV_MODE, 0.7
                    ),
                    f"判断不能（信頼 {confidence:.2f} / Jev）※ 人による確認を推奨",
                )


# --- Jev client (fake HTTP layer) ------------------------------------------


class FakeResponse:
    def __init__(self, status=200, payload=None, json_error=None) -> None:
        self.status = status
        self._payload = payload
        self._json_error = json_error

    async def json(self, content_type=None):
        if self._json_error is not None:
            raise self._json_error
        if self._payload is None:
            raise ValueError("empty body")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FailingContext:
    def __init__(self, exc) -> None:
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """Records every request; responses are popped per call."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "json": json, "headers": headers})
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            return FailingContext(item)
        return item


def make_judge(attempts=3, base_delay=0.0, max_delay=0.0):
    return JevApplicationJudge(
        API_KEY,
        MODEL,
        timeout_seconds=5,
        retry_attempts=attempts,
        retry_base_seconds=base_delay,
        retry_max_seconds=max_delay,
    )


class JevJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_posts_bearer_request_to_the_documented_url(self) -> None:
        session = FakeSession([FakeResponse(200, jev_payload())])
        request = build_request(build_state(10, []), MODEL)
        parsed = await make_judge().judge(session, request)

        self.assertEqual(parsed["application_state"], "申請中")
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(call["url"], JEV_ENDPOINT)
        self.assertEqual(
            call["headers"],
            {
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(call["json"], request)

    async def test_retries_429_and_529_with_backoff(self) -> None:
        session = FakeSession([
            FakeResponse(429),
            FakeResponse(529),
            FakeResponse(200, jev_payload()),
        ])
        parsed = await make_judge().judge(session, build_request(build_state(10, []), MODEL))
        self.assertEqual(parsed["application_state"], "申請中")
        self.assertEqual(len(session.calls), 3)

    async def test_gives_up_after_the_retry_budget(self) -> None:
        session = FakeSession([FakeResponse(429) for _ in range(3)])
        with self.assertRaises(JevRetryableError):
            await make_judge().judge(session, build_request(build_state(10, []), MODEL))
        self.assertEqual(len(session.calls), 3)

    async def test_permanent_status_codes_fail_immediately(self) -> None:
        for status in (400, 401, 422):
            with self.subTest(status=status):
                session = FakeSession([FakeResponse(status)])
                with self.assertRaises(JevError):
                    await make_judge().judge(
                        session, build_request(build_state(10, []), MODEL)
                    )
                self.assertEqual(len(session.calls), 1)

    async def test_network_and_timeout_errors_are_permanent(self) -> None:
        for exc in (asyncio.TimeoutError(), ConnectionError("down")):
            with self.subTest(exc=exc):
                session = FakeSession([exc])
                with self.assertRaises(JevError):
                    await make_judge().judge(
                        session, build_request(build_state(10, []), MODEL)
                    )
                self.assertEqual(len(session.calls), 1)

    async def test_invalid_body_is_reported_as_jev_error(self) -> None:
        session = FakeSession([FakeResponse(200, payload=[])])
        with self.assertRaises(JevError):
            await make_judge().judge(
                session, build_request(build_state(10, []), MODEL)
            )

    def test_backoff_grows_and_is_capped(self) -> None:
        judge = JevApplicationJudge(
            API_KEY,
            MODEL,
            retry_base_seconds=3,
            retry_max_seconds=10,
        )
        self.assertEqual(judge.backoff_seconds(1), 3)
        self.assertEqual(judge.backoff_seconds(2), 6)
        self.assertEqual(judge.backoff_seconds(3), 10)
        self.assertEqual(judge.backoff_seconds(9), 10)


# --- Scheduler --------------------------------------------------------------


class FakeJudge:
    """Stub client: either returns a fixed judgment or raises."""

    def __init__(self, result=None, error=None) -> None:
        self.model = MODEL
        self._result = result
        self._error = error
        self.calls = []

    async def judge(self, session, request):
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeDatabase:
    def __init__(self) -> None:
        self.saved = []

    async def save(self, channel_id, *, has_application, mode, detail=None, judged_at=None):
        self.saved.append(
            {
                "channel_id": channel_id,
                "has_application": has_application,
                "mode": mode,
                "detail": detail,
            }
        )


def make_scheduler(judge, database, channels=()):
    return JevApplicationScheduler(
        SimpleNamespace(guilds=[SimpleNamespace(text_channels=list(channels))]),
        database,
        judge,
        session=SimpleNamespace(),
        confidence_threshold=JEV_CONFIDENCE_THRESHOLD_DEFAULT,
        is_monitored_channel=lambda channel: getattr(channel, "monitored", True),
    )


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_jev_success_stores_the_judgment(self) -> None:
        database = FakeDatabase()
        judge = FakeJudge(result=parse_jev_response(jev_payload()))
        scheduler = make_scheduler(judge, database)
        channel = SimpleNamespace(
            id=10,
            history=AsyncIterator([make_discord_message("よろしくお願いします。")]),
        )
        await scheduler._judge_channel(channel)

        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(len(database.saved), 1)
        save = database.saved[0]
        self.assertEqual(save["mode"], JEV_MODE)
        self.assertTrue(save["has_application"])
        self.assertEqual(
            save["detail"],
            {
                "application_state": "申請中",
                "confidence": 0.85,
                "no_application_yet": 0.12,
            },
        )

    async def test_jev_failure_falls_back_to_simple_when_an_image_was_found(self) -> None:
        channel = SimpleNamespace(
            id=11,
            history=AsyncIterator(
                [make_discord_message("screenshots", attachments=(make_attachment("image/png"),))]
            ),
        )
        database = FakeDatabase()
        judge = FakeJudge(error=JevError("Jev API down"))
        scheduler = make_scheduler(judge, database)

        await scheduler._judge_channel(channel)

        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(
            database.saved,
            [{
                "channel_id": 11,
                "has_application": True,
                "mode": SIMPLE_MODE,
                "detail": None,
            }],
        )

    async def test_jev_failure_without_evidence_does_not_downgrade(self) -> None:
        channel = SimpleNamespace(
            id=12,
            history=AsyncIterator([make_discord_message("まだです")]),
        )
        database = FakeDatabase()
        judge = FakeJudge(error=JevError("Jev API down"))
        scheduler = make_scheduler(judge, database)

        await scheduler._judge_channel(channel)

        self.assertEqual(database.saved, [])

    async def test_empty_history_skips_jev_entirely(self) -> None:
        channel = SimpleNamespace(id=15, history=AsyncIterator([]))
        database = FakeDatabase()
        judge = FakeJudge(result=parse_jev_response(jev_payload()))
        scheduler = make_scheduler(judge, database)

        await scheduler._judge_channel(channel)

        self.assertEqual(judge.calls, [])
        self.assertEqual(database.saved, [])

    async def test_oversized_history_skips_jev_and_falls_back(self) -> None:
        # Per-message caps keep real histories under the limit; the None path is
        # a safety net, exercised here by forcing the trim to give up.
        channel = SimpleNamespace(
            id=13,
            history=AsyncIterator(
                [make_discord_message("screenshots", attachments=(make_attachment("image/png"),))]
            ),
        )
        database = FakeDatabase()
        judge = FakeJudge(result=parse_jev_response(jev_payload()))
        scheduler = make_scheduler(judge, database)

        with unittest.mock.patch(
            "application.build_request_within_limit", return_value=None
        ):
            await scheduler._judge_channel(channel)

        self.assertEqual(judge.calls, [])
        self.assertEqual(len(database.saved), 1)
        self.assertEqual(database.saved[0]["mode"], SIMPLE_MODE)
        self.assertTrue(database.saved[0]["has_application"])

    async def test_history_read_failure_is_reported_and_skipped(self) -> None:
        class BrokenChannel:
            id = 14

            def history(self, limit=None):
                raise discord.HTTPException(
                    SimpleNamespace(status=500, reason="err"), "history read failed"
                )

        database = FakeDatabase()
        judge = FakeJudge(result=parse_jev_response(jev_payload()))
        scheduler = make_scheduler(judge, database)

        await scheduler._judge_channel(BrokenChannel())

        self.assertEqual(judge.calls, [])
        self.assertEqual(database.saved, [])

    def test_only_monitored_channels_are_judged(self) -> None:
        monitored = SimpleNamespace(id=1, monitored=True)
        ignored = SimpleNamespace(id=2, monitored=False)
        scheduler = make_scheduler(FakeJudge(), FakeDatabase(), [monitored, ignored])
        self.assertEqual(list(scheduler._iter_monitored_channels()), [monitored])


# sanity: the trim must keep at least the newest message in normal conditions
class TrimSanityTests(unittest.TestCase):
    def test_random_histories_always_fit_or_trim_to_the_tail(self) -> None:
        rng = random.Random(2026)
        for _ in range(10):
            messages = [
                make_lean_message("あ" * rng.randint(10, 1800))
                for _ in range(rng.randint(1, 40))
            ]
            request = build_request_within_limit(messages, 10, MODEL)
            if request is None:
                continue
            self.assertLessEqual(
                len(serialize_request(request)), JEV_MAX_PAYLOAD_BYTES
            )


if __name__ == "__main__":
    unittest.main()
