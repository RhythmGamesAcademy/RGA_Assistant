import json
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from config import parse_todo_endpoint_settings
from todotodo import (
    ASSISTANT_NAME,
    DECISION_APPROVAL,
    DECISION_REJECT,
    TODO_MAX_MEMO_LENGTH,
    TODO_MAX_TITLE_LENGTH,
    TodoTodoClient,
    TodoTodoError,
    build_decide_memo,
    build_todo_description,
    build_todo_title,
    build_todo_registration_failure_embed,
    count_todo_holders,
    decide_threshold,
    has_todo_permission,
)


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._text = json.dumps(payload) if payload is not None else ""

    async def text(self):
        return self._text


class FakeContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        outcome = self.outcomes.pop(0) if self.outcomes else aiohttp.ClientError()
        if isinstance(outcome, Exception):
            raise outcome
        return FakeContext(outcome)


def make_client(outcomes, token="list-token"):
    session = FakeSession(outcomes)
    client = TodoTodoClient(session, "https://todo.example", token)
    return client, session


class TodoPermissionTests(unittest.TestCase):
    def test_administrator_always_allowed(self) -> None:
        self.assertTrue(has_todo_permission(True, set(), ()))

    def test_empty_role_ids_allow_nobody_else(self) -> None:
        self.assertFalse(has_todo_permission(False, {10, 20}, ()))

    def test_role_intersection_is_required(self) -> None:
        self.assertTrue(has_todo_permission(False, {10, 20}, (20, 30)))
        self.assertFalse(has_todo_permission(False, {10}, (20,)))


class TodoThresholdTests(unittest.TestCase):
    def test_threshold_is_ceil_of_half(self) -> None:
        self.assertEqual(decide_threshold(0), 1)
        self.assertEqual(decide_threshold(1), 1)
        self.assertEqual(decide_threshold(2), 1)
        self.assertEqual(decide_threshold(3), 2)
        self.assertEqual(decide_threshold(4), 2)
        self.assertEqual(decide_threshold(5), 3)
        self.assertEqual(decide_threshold(6), 3)

    def test_counts_holders_without_excluded_users(self) -> None:
        members = [(1, {10}), (2, {10, 20}), (3, {30}), (4, set())]
        self.assertEqual(count_todo_holders(members, (10,)), 2)

    def test_excluded_users_do_not_count(self) -> None:
        members = [(1, {10}), (2, {10})]
        self.assertEqual(count_todo_holders(members, (10,), (1,)), 1)
        self.assertEqual(count_todo_holders(members, (10,), (1, 2)), 0)

    def test_no_required_roles_counts_nobody(self) -> None:
        self.assertEqual(count_todo_holders([(1, {10})], ()), 0)


class TodoTextBuildersTests(unittest.TestCase):
    def test_title_is_truncated_to_the_api_limit(self) -> None:
        title = build_todo_title("ち" * 130)
        self.assertEqual(len(title), TODO_MAX_TITLE_LENGTH)
        self.assertTrue(title.endswith("…"))

    def test_short_title_is_kept_as_is(self) -> None:
        self.assertEqual(build_todo_title("ticket-10"), "ticket-10")

    def test_description_points_back_to_the_ticket(self) -> None:
        self.assertEqual(
            build_todo_description(20, 10),
            "チケット: https://discord.com/channels/20/10",
        )

    def test_memo_contains_decision_count_and_executor(self) -> None:
        self.assertEqual(
            build_decide_memo(DECISION_APPROVAL, 2, "運営A", 123),
            "承認｜判断人数: 2名｜実行者: 運営A (ID: 123)",
        )
        self.assertEqual(
            build_decide_memo(DECISION_REJECT, 1, "運営B", 456),
            "却下｜判断人数: 1名｜実行者: 運営B (ID: 456)",
        )

    def test_memo_records_a_changed_decision(self) -> None:
        self.assertEqual(
            build_decide_memo(
                DECISION_APPROVAL, 1, "運営A", 123,
                previous_decision=DECISION_REJECT,
            ),
            "却下から承認に変更｜判断人数: 1名｜実行者: 運営A (ID: 123)",
        )
        self.assertEqual(
            build_decide_memo(
                DECISION_REJECT, 1, "運営A", 123,
                previous_decision=DECISION_APPROVAL,
            ),
            "承認から却下に変更｜判断人数: 1名｜実行者: 運営A (ID: 123)",
        )

    def test_memo_with_the_same_previous_decision_is_plain(self) -> None:
        self.assertEqual(
            build_decide_memo(
                DECISION_APPROVAL, 1, "運営A", 123,
                previous_decision=DECISION_APPROVAL,
            ),
            "承認｜判断人数: 1名｜実行者: 運営A (ID: 123)",
        )

    def test_memo_is_truncated_to_the_api_limit(self) -> None:
        memo = build_decide_memo(DECISION_APPROVAL, 1, "な" * 2100, 1)
        self.assertLessEqual(len(memo), TODO_MAX_MEMO_LENGTH)

    def test_memo_supports_threshold_style_count(self) -> None:
        self.assertEqual(
            build_decide_memo(DECISION_APPROVAL, "2/3", "運営A", 123),
            "承認｜判断人数: 2/3名｜実行者: 運営A (ID: 123)",
        )

    def test_failure_embed_contains_ticket_link_and_error(self) -> None:
        embed = build_todo_registration_failure_embed("10", "20", 6, "boom")
        text = "\n".join(
            [embed.title or "", embed.description or ""]
            + [f"{f.name} {f.value}" for f in embed.fields]
        )
        self.assertIn("https://discord.com/channels/20/10", text)
        self.assertIn("6", text)
        self.assertIn("boom", text)


class TodoEndpointSettingsTests(unittest.TestCase):
    def test_both_values_enable_the_integration(self) -> None:
        base_url, token = parse_todo_endpoint_settings(
            " https://todo.example ", " token "
        )
        self.assertEqual(base_url, "https://todo.example")
        self.assertEqual(token, "token")

    def test_no_values_disable_the_integration(self) -> None:
        self.assertEqual(parse_todo_endpoint_settings(None, None), (None, None))
        self.assertEqual(parse_todo_endpoint_settings("", "  "), (None, None))

    def test_half_configured_state_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_todo_endpoint_settings("https://todo.example", None)
        with self.assertRaises(ValueError):
            parse_todo_endpoint_settings(None, "token")


class TodoTodoClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_assistant_member_reuses_existing_member(self) -> None:
        client, session = make_client(
            [FakeResponse(200, {"users": [{"id": "u1", "name": ASSISTANT_NAME}]})]
        )
        self.assertEqual(await client.ensure_assistant_member(), "u1")
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(session.requests[0]["method"], "GET")

    async def test_ensure_assistant_member_joins_when_missing(self) -> None:
        client, session = make_client(
            [
                FakeResponse(200, {"users": [{"id": "u1", "name": "other"}]}),
                FakeResponse(201, {"userId": "u2", "name": ASSISTANT_NAME}),
            ]
        )
        self.assertEqual(await client.ensure_assistant_member(), "u2")
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(session.requests[1]["method"], "POST")
        self.assertEqual(
            session.requests[1]["json"], {"name": ASSISTANT_NAME}
        )

    async def test_create_todo_posts_expected_payload(self) -> None:
        client, session = make_client([FakeResponse(201, {"id": "todo-1"})])
        client.assistant_user_id = "user-9"
        self.assertEqual(
            await client.create_todo("ticket-10", "チケット: url"), "todo-1"
        )
        request = session.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertTrue(request["url"].endswith("/api/lists/list-token/todos"))
        self.assertEqual(
            request["json"],
            {
                "title": "ticket-10",
                "status": "todo",
                "priority": "medium",
                "description": "チケット: url",
                "userId": "user-9",
            },
        )

    async def test_complete_todo_patches_only_the_status(self) -> None:
        client, session = make_client([FakeResponse(200, {"revision": 13})])
        client.assistant_user_id = "user-9"
        await client.complete_todo("todo-1")
        request = session.requests[0]
        self.assertEqual(request["method"], "PATCH")
        self.assertTrue(
            request["url"].endswith("/api/lists/list-token/todos/todo-1")
        )
        self.assertEqual(
            request["json"],
            {"status": "done", "userId": "user-9"},
        )

    async def test_add_memo_posts_the_body(self) -> None:
        client, session = make_client([FakeResponse(201, {"id": "memo-1"})])
        client.assistant_user_id = "user-9"
        await client.add_memo("todo-1", "承認｜判断人数: 1名")
        request = session.requests[0]
        self.assertTrue(
            request["url"].endswith("/api/lists/list-token/todos/todo-1/memos")
        )
        self.assertEqual(
            request["json"],
            {"body": "承認｜判断人数: 1名", "userId": "user-9"},
        )

    async def test_retries_5xx_with_backoff(self) -> None:
        client, session = make_client(
            [
                FakeResponse(503, {"error": "internal server error"}),
                FakeResponse(200, {"revision": 3}),
            ]
        )
        with patch("todotodo.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            self.assertEqual(await client._request("GET", ""), {"revision": 3})
        self.assertEqual(len(session.requests), 2)
        sleep_mock.assert_awaited_once_with(1.0)

    async def test_client_errors_are_retried(self) -> None:
        client, session = make_client(
            [aiohttp.ClientError(), FakeResponse(200, {"revision": 3})]
        )
        with patch("todotodo.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await client._request("GET", ""), {"revision": 3})
        self.assertEqual(len(session.requests), 2)

    async def test_gives_up_after_all_retries(self) -> None:
        client, session = make_client(
            [FakeResponse(502, {"error": "internal server error"}) for _ in range(3)]
        )
        with patch("todotodo.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with self.assertRaises(TodoTodoError):
                await client._request("GET", "")
        self.assertEqual(len(session.requests), 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.await_args_list], [1.0, 2.0]
        )

    async def test_4xx_errors_are_not_retried(self) -> None:
        client, session = make_client(
            [FakeResponse(404, {"error": "not found"})]
        )
        with patch("todotodo.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with self.assertRaises(TodoTodoError):
                await client._request("GET", "")
        self.assertEqual(len(session.requests), 1)
        sleep_mock.assert_not_awaited()

    async def test_error_messages_never_leak_the_token(self) -> None:
        client, session = make_client([aiohttp.ClientError()], token="secret-token")
        with self.assertRaises(TodoTodoError) as caught:
            await client._request("GET", "")
        self.assertNotIn("secret-token", str(caught.exception))

        client, session = make_client(
            [FakeResponse(404, {"error": "not found"})], token="secret-token"
        )
        with self.assertRaises(TodoTodoError) as caught:
            await client._request("GET", "")
        self.assertNotIn("secret-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
