"""TodoTodo (共有TODOアプリ) 連携 (Phase 5).

REST API (Hono + Neon + Vercel) をサーバー側から操作するクライアント、
申請画像検知時のTODO自動登録ループ、決着 slash コマンドを提供する。

認可はリストごとのトークン (``RGA_TODO_LIST_TOKEN``) のみで行われるため、
トークンはログやチャット・エラーメッセージに出力しない。Base URL も
トークンを含む完全な URL を絶対にログへ出さないこと。
"""

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import discord
import psycopg
from discord import app_commands
from discord.ext import tasks

from config import Config
from database import TodoDatabase
from embeds import (
    COLOR_ERROR,
    COLOR_INFO,
    MAX_FIELD_VALUE,
    error_embed,
    success_embed,
    truncate,
)

logger = logging.getLogger("rga_assistant")

# Bot がリストに参加するときのメンバー名 (操作者帰属の明示に使う)。
ASSISTANT_NAME = "RGA Assistant"

# API の入力上限 (仕様: title 120字 / description・メモ 2000字)。
TODO_MAX_TITLE_LENGTH = 120
TODO_MAX_DESCRIPTION_LENGTH = 2000
TODO_MAX_MEMO_LENGTH = 2000
TODO_TIMEOUT_SECONDS = 15.0

# 5xx・通信エラーに対する指数バックオフ再試行 (仕様: 5xx は数回だけ再試行してよい)。
TODO_RETRY_ATTEMPTS = 3
TODO_RETRY_BASE_SECONDS = 1.0
TODO_RETRY_MAX_SECONDS = 8.0

# TODO自動登録の再試行ループ間隔 (秒)。
TODO_TICK_SECONDS = 60
TODO_RETRY_SECONDS = 60.0

# 期限リマインドのチェックループ間隔 (秒)。
TODO_REMIND_TICK_SECONDS = 60

# Bot 内の JST 固定 (main.py と同じ扱い)。
JST = timezone(timedelta(hours=9), name="JST")

DECISION_APPROVAL = "approval"
DECISION_REJECT = "reject"


class TodoTodoError(Exception):
    """Raised when the TodoTodo API cannot complete an operation."""


def has_todo_permission(
    is_administrator: bool,
    member_role_ids: set[int],
    required_role_ids: tuple[int, ...],
) -> bool:
    """Mirror the lecture permission rule for the TodoTodo commands."""
    if is_administrator:
        return True
    if not required_role_ids:
        return False
    return bool(member_role_ids.intersection(required_role_ids))


def count_todo_holders(
    member_role_ids: Iterable[tuple[int, set[int]]],
    required_role_ids: tuple[int, ...],
    excluded_user_ids: tuple[int, ...] = (),
) -> int:
    """Count distinct users holding at least one required role.

    Excluded users (``RGA_TODO_EXCLUDED_USER_IDS``) do not count toward the
    denominator of the decision threshold.
    """
    excluded = set(excluded_user_ids)
    required = set(required_role_ids)
    count = 0
    for user_id, role_ids in member_role_ids:
        if user_id in excluded:
            continue
        if role_ids.intersection(required):
            count += 1
    return count


def decide_threshold(holders: int) -> int:
    """Return the majority threshold (ceil of holders / 2).

    With no known holders the threshold stays at 1 so a single decision can
    still settle the TODO (safe side).
    """
    if holders <= 0:
        return 1
    return (holders + 1) // 2


def find_decide_channel(
    ticket_category_id: int | None,
    decide_channels: tuple[tuple[int, int], ...],
) -> int | None:
    """Return the decide channel mapped for the ticket's category, if any."""
    if ticket_category_id is None:
        return None
    return dict(decide_channels).get(ticket_category_id)


def collect_deadline_reminders(
    payload: Any,
    now: datetime,
) -> list[dict[str, Any]]:
    """Pick the unfinished todos needing a deadline reminder.

    A todo qualifies when it is not ``done`` and has a ``deadline`` whose
    date (JST) is either tomorrow (reminder one day ahead) or already past
    (overdue). Todos without a deadline and malformed dates are skipped.
    ``now`` must be timezone-aware; comparisons use the JST date.
    """
    todos = (payload or {}).get("todos") or []
    today = now.astimezone(JST).date()
    tomorrow = today + timedelta(days=1)
    items: list[dict[str, Any]] = []
    for todo in todos:
        if not isinstance(todo, dict) or todo.get("status") == "done":
            continue
        raw_deadline = todo.get("deadline")
        if not raw_deadline:
            continue
        try:
            deadline = datetime.fromisoformat(
                str(raw_deadline).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        deadline_jst = deadline.astimezone(JST)
        due_date = deadline_jst.date()
        if due_date == tomorrow:
            reason = "期限前日"
        elif due_date < today:
            reason = f"期限切れ（{(today - due_date).days}日超過）"
        else:
            continue
        items.append(
            {
                "id": todo.get("id"),
                "title": todo.get("title") or "",
                "description": todo.get("description") or "",
                "deadline": deadline_jst,
                "status": todo.get("status") or "",
                "reason": reason,
            }
        )
    return items


def build_todo_remind_embed(items: list[dict[str, Any]]) -> discord.Embed:
    """Build the daily deadline reminder embed from collected items."""
    embed = discord.Embed(
        title="トドTODO 期限リマインド",
        description=(
            "期限の1日前または期限切れの未決着TODOがあります。"
            "決着用チャンネルで `/todo decide` を実行してください。"
        ),
        color=COLOR_INFO,
    )
    for item in items:
        link = truncate(item.get("description") or "", MAX_FIELD_VALUE)
        value = (
            f"期限: {item['deadline'].strftime('%Y/%m/%d %H:%M')} (JST)\n"
            f"状態: {item['reason']}\n{link}"
        )
        embed.add_field(
            name=truncate(item.get("title") or "(無題)", 100),
            value=truncate(value, MAX_FIELD_VALUE),
            inline=False,
        )
    return embed


def build_new_todo_embed(todos: list[dict[str, Any]]) -> discord.Embed:
    """Build the embed announcing newly created TodoTodo items."""
    embed = discord.Embed(
        title="トドTODO 新規TODO",
        description="トドTODO側で新しいTODOが登録されました。",
        color=COLOR_INFO,
    )
    for todo in todos[:25]:
        link = truncate(todo.get("description") or "", MAX_FIELD_VALUE) or "-"
        embed.add_field(
            name=truncate(todo.get("title") or "(無題)", 100),
            value=link,
            inline=False,
        )
    return embed


def build_ops_mention(config: Config) -> str | None:
    """Return the operations mention string (role preferred), if configured."""
    if config.operations_mention_role_id is not None:
        return f"<@&{config.operations_mention_role_id}>"
    if config.operations_mention_user_id is not None:
        return f"<@{config.operations_mention_user_id}>"
    return None


def build_todo_title(channel_name: str) -> str:
    """Build the TodoTodo title from the ticket channel name."""
    return truncate(channel_name, TODO_MAX_TITLE_LENGTH)


def build_todo_description(guild_id: int | str, channel_id: int | str) -> str:
    """Build the TodoTodo description pointing back to the ticket channel."""
    return f"チケット: https://discord.com/channels/{guild_id}/{channel_id}"


def _decision_label(decision: str) -> str:
    return "承認" if decision == DECISION_APPROVAL else "却下"


def build_decide_memo(
    decision: str,
    judge_count: int | str,
    executor_name: str,
    executor_id: int,
    previous_decision: str | None = None,
) -> str:
    """Build the memo appended to the TodoTodo item on a decide command.

    When the executor changes their earlier decision, the memo records the
    change (e.g. ``却下から承認に変更``) instead of a plain decision.
    ``judge_count`` may be a ``2/3`` style string in threshold mode.
    """
    label = _decision_label(decision)
    if previous_decision is not None and previous_decision != decision:
        head = f"{_decision_label(previous_decision)}から{label}に変更"
    else:
        head = label
    memo = f"{head}｜判断人数: {judge_count}名｜実行者: {executor_name} (ID: {executor_id})"
    return truncate(memo, TODO_MAX_MEMO_LENGTH)


def build_todo_registration_failure_embed(
    channel_id: str,
    guild_id: str,
    attempts: int,
    error: str,
) -> discord.Embed:
    embed = discord.Embed(title="トドTODO登録失敗", color=COLOR_ERROR)
    embed.add_field(
        name="チケット",
        value=f"https://discord.com/channels/{guild_id}/{channel_id}",
        inline=False,
    )
    embed.add_field(name="試行回数", value=str(attempts), inline=True)
    embed.add_field(
        name="理由", value=truncate(error, MAX_FIELD_VALUE) or "-", inline=False
    )
    return embed


class TodoTodoClient:
    """HTTP client for the TodoTodo REST API.

    Only the safe operations required by the bot are implemented (list read,
    member join, TODO creation, TODO update, memo append). Dangerous
    endpoints (list deletion, member deletion, TODO deletion) are
    intentionally not wrapped.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        list_token: str,
    ) -> None:
        self._session = session
        self._base_url = base_url.strip().rstrip("/")
        self._list_token = list_token
        self.assistant_user_id: str | None = None

    async def ensure_assistant_member(self) -> str:
        """Make sure ``RGA Assistant`` is a list member and return its id."""
        data = await self._request("GET", "")
        for user in (data or {}).get("users") or []:
            if user.get("name") == ASSISTANT_NAME and user.get("id"):
                self.assistant_user_id = user["id"]
                return self.assistant_user_id
        joined = await self._request("POST", "/join", {"name": ASSISTANT_NAME})
        user_id = (joined or {}).get("userId")
        if not user_id:
            raise TodoTodoError("TodoTodo API returned no userId after joining.")
        self.assistant_user_id = user_id
        return user_id

    async def create_todo(self, title: str, description: str = "") -> str:
        """Create a TODO item and return its id."""
        payload: dict[str, Any] = {
            "title": truncate(title, TODO_MAX_TITLE_LENGTH),
            "status": "todo",
            "priority": "medium",
            "description": truncate(description, TODO_MAX_DESCRIPTION_LENGTH),
        }
        if self.assistant_user_id is not None:
            payload["userId"] = self.assistant_user_id
        data = await self._request("POST", "/todos", payload)
        todo_id = (data or {}).get("id")
        if not todo_id:
            raise TodoTodoError("TodoTodo API returned no todo id.")
        return todo_id

    async def complete_todo(self, todo_id: str) -> None:
        """Update one TODO item to ``status=done`` (partial update)."""
        payload: dict[str, Any] = {"status": "done"}
        if self.assistant_user_id is not None:
            payload["userId"] = self.assistant_user_id
        await self._request("PATCH", f"/todos/{todo_id}", payload)

    async def add_memo(self, todo_id: str, memo: str) -> None:
        """Append one memo to a TODO item (memos are append-only)."""
        payload: dict[str, Any] = {"body": truncate(memo, TODO_MAX_MEMO_LENGTH)}
        if self.assistant_user_id is not None:
            payload["userId"] = self.assistant_user_id
        await self._request("POST", f"/todos/{todo_id}/memos", payload)

    async def list_todos(self) -> Any:
        """Fetch the list payload (todos included) from the API."""
        return await self._request("GET", "")

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Send one request, retrying 5xx and network errors with backoff.

        The URL contains the list token, so neither the URL nor raw
        ``aiohttp`` exception messages (which embed the URL) may be logged
        or stored; only the HTTP status or the exception class name is kept.
        """
        url = f"{self._base_url}/api/lists/{self._list_token}{path}"
        last_error: TodoTodoError | None = None
        for attempt in range(1, TODO_RETRY_ATTEMPTS + 1):
            try:
                async with self._session.request(
                    method,
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=TODO_TIMEOUT_SECONDS),
                ) as response:
                    text = await response.text()
                    if 200 <= response.status < 300:
                        if not text:
                            return None
                        try:
                            return json.loads(text)
                        except ValueError as exc:
                            raise TodoTodoError(
                                f"TodoTodo API returned invalid JSON: "
                                f"{type(exc).__name__}"
                            ) from exc
                    if response.status < 500:
                        raise TodoTodoError(
                            f"TodoTodo API returned HTTP {response.status}."
                        )
                    last_error = TodoTodoError(
                        f"TodoTodo API returned HTTP {response.status}."
                    )
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                last_error = TodoTodoError(
                    f"TodoTodo API communication failed: {type(exc).__name__}"
                )
            if attempt < TODO_RETRY_ATTEMPTS:
                delay = min(
                    TODO_RETRY_MAX_SECONDS,
                    TODO_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                )
                logger.warning(
                    "TodoTodo API request failed (attempt %s/%s); "
                    "retrying in %.1fs",
                    attempt,
                    TODO_RETRY_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error


class TodoCommands(app_commands.Group):
    """Slash commands under ``/todo`` (Phase 5: TodoTodo integration)."""

    def __init__(
        self,
        config: Config,
        database: TodoDatabase | None,
        client: TodoTodoClient | None,
    ) -> None:
        super().__init__(
            name="todo",
            description="トドTODO連携の操作を行います。",
        )
        self.config = config
        self.database = database
        self.client = client

    async def _ensure_permission(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("このコマンドはサーバー内でのみ実行できます。"),
                ephemeral=True,
            )
            return False
        if member.id in self.config.todo_excluded_user_ids:
            await interaction.response.send_message(
                embed=error_embed(
                    "あなたはTODO判断の対象から除外されています。運営に確認してください。"
                ),
                ephemeral=True,
            )
            return False
        if has_todo_permission(
            member.guild_permissions.administrator,
            {role.id for role in member.roles},
            self.config.todo_role_ids,
        ):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "TODOを操作する権限がありません。運営に確認してください。"
            ),
            ephemeral=True,
        )
        return False

    def _decide_channel_error(
        self,
        interaction: discord.Interaction,
        ticket: discord.TextChannel,
    ) -> str | None:
        """Return the error message when the decide channel is wrong.

        ``/todo decide`` runs only from the per-category channel configured in
        ``RGA_TODO_DECIDE_CHANNELS``, never inside the ticket itself, so
        concurrent applications stay isolated.
        """
        if not self.config.todo_decide_channels:
            return (
                "`/todo decide` 用のチャンネルが設定されていません。"
                "`.setting` の `RGA_TODO_DECIDE_CHANNELS` を設定してください。"
            )
        decide_channel_id = find_decide_channel(
            ticket.category_id, self.config.todo_decide_channels
        )
        if decide_channel_id is None:
            return (
                "このチケットのカテゴリには `/todo decide` 用のチャンネルが"
                "設定されていません。設定を確認してください。"
            )
        if decide_channel_id != interaction.channel_id:
            return (
                f"このチケットの決着は <#{decide_channel_id}> で行ってください。"
            )
        if decide_channel_id == ticket.id:
            return "チケットチャンネルと決着用チャンネルが同じです。設定を確認してください。"
        return None

    @app_commands.command(
        name="decide",
        description="チケットのTODOを決着させます（statusをdoneに更新し、メモを追記します）。",
    )
    @app_commands.describe(
        decision="決着の種類（承認／却下）",
        ticket="決着する申請のチケットチャンネル",
    )
    @app_commands.choices(
        decision=[
            app_commands.Choice(name="承認", value=DECISION_APPROVAL),
            app_commands.Choice(name="却下", value=DECISION_REJECT),
        ],
    )
    async def decide(
        self,
        interaction: discord.Interaction,
        decision: app_commands.Choice[str],
        ticket: discord.TextChannel,
    ) -> None:
        if not await self._ensure_permission(interaction):
            return
        if self.database is None or self.client is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "トドTODO連携が有効になっていません。"
                    "`RGA_TODO_BASE_URL` と `RGA_TODO_LIST_TOKEN` を "
                    "`.env` に設定してください。"
                ),
                ephemeral=True,
            )
            return
        decide_error = self._decide_channel_error(interaction, ticket)
        if decide_error is not None:
            await interaction.response.send_message(
                embed=error_embed(decide_error),
                ephemeral=True,
            )
            return

        channel_id = str(ticket.id)
        try:
            row = await self.database.get_todo(channel_id)
        except psycopg.Error:
            logger.exception("Failed to load TodoTodo item for %s", channel_id)
            await interaction.response.send_message(
                embed=error_embed("TODO情報の取得に失敗しました。ログを確認してください。"),
                ephemeral=True,
            )
            return
        if row is None or not row.get("todo_id"):
            embed = error_embed(
                "指定されたチケットにはトドTODOのTODOが登録されていないか、"
                "登録処理が完了していません。"
            )
            embed.add_field(
                name="チケット", value=f"<#{ticket.id}>", inline=True
            )
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
            )
            return

        label = _decision_label(decision.value)
        try:
            previous, judge_count = await self.database.get_decision(
                channel_id, interaction.user.id
            )
        except psycopg.Error:
            logger.exception("Failed to load TODO decision for %s", channel_id)
            await interaction.response.send_message(
                embed=error_embed("TODO情報の取得に失敗しました。ログを確認してください。"),
                ephemeral=True,
            )
            return

        if previous == decision.value:
            # Same decision recorded again: nothing changes on the TodoTodo side.
            embed = discord.Embed(
                title="もう判定しました",
                description=f"{label}はすでに記録されています。",
                color=COLOR_INFO,
            )
            embed.add_field(
                name="チケット", value=f"<#{ticket.id}>", inline=True
            )
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        # Threshold mode is active only when decide roles are configured;
        # without them the first decision settles the TODO as before.
        threshold: int | None = None
        guild = interaction.guild
        if self.config.todo_role_ids and guild is not None:
            holders = count_todo_holders(
                ((m.id, {r.id for r in m.roles}) for m in guild.members if not m.bot),
                self.config.todo_role_ids,
                self.config.todo_excluded_user_ids,
            )
            threshold = decide_threshold(holders)

        # The API calls below include retries and can take several seconds.
        await interaction.response.defer(thinking=True)
        try:
            judge_count = await self.database.record_decision(
                channel_id, str(interaction.user.id), decision.value
            )
            tallies = await self.database.decision_tallies(channel_id)
            already_decided = bool(row.get("decided"))
            settle = False
            if not already_decided:
                if threshold is None:
                    settle = True
                else:
                    settle = max(
                        tallies.get(DECISION_APPROVAL, 0),
                        tallies.get(DECISION_REJECT, 0),
                    ) >= threshold
                if settle:
                    await self.client.complete_todo(row["todo_id"])
                    await self.database.mark_todo_decided(channel_id)
            memo = build_decide_memo(
                decision.value,
                f"{judge_count}/{threshold}" if threshold is not None else judge_count,
                interaction.user.display_name,
                interaction.user.id,
                previous_decision=previous,
            )
            await self.client.add_memo(row["todo_id"], memo)
        except TodoTodoError as exc:
            logger.error("Failed to update TodoTodo item %s: %s", channel_id, exc)
            await interaction.followup.send(
                embed=error_embed(
                    "トドTODOの更新に失敗しました。しばらくしてからもう一度お試しください。"
                ),
                ephemeral=True,
            )
            return
        except psycopg.Error:
            logger.exception("Failed to record TODO decision for %s", channel_id)
            await interaction.followup.send(
                embed=error_embed("判断の記録に失敗しました。ログを確認してください。"),
                ephemeral=True,
            )
            return

        if settle:
            tail = "トドTODOのTODOを決着させ、メモを追記しました。"
        elif already_decided:
            tail = "TODOはすでに決着済みのため、メモのみ追記しました。"
        else:
            tail = "判断人数がしきい値に達していないため、TODOはまだ決着していません。"
        if previous is None:
            title = f"{label}を記録しました"
            description = tail
        else:
            title = "判断を変更しました"
            description = f"{_decision_label(previous)}から{label}へ変更し、{tail}"
        if threshold is not None:
            progress = f"判断人数: {judge_count}/{threshold}名"
        else:
            progress = f"判断人数: {judge_count}名"
        embed = success_embed(title, f"{description}\n{progress}")
        embed.add_field(
            name="チケット", value=f"<#{ticket.id}>", inline=True
        )
        embed.add_field(
            name="TODOステータス",
            value="done" if (settle or already_decided) else "todo",
            inline=True,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class TodoRegistrationScheduler:
    """Registers TodoTodo items for tickets with a detected application image."""

    def __init__(
        self,
        client: discord.Client,
        config: Config,
        database: TodoDatabase,
        todotodo: TodoTodoClient,
    ) -> None:
        self.client = client
        self.config = config
        self.database = database
        self.todotodo = todotodo

    def start(self) -> None:
        self.registration_loop.start()

    @tasks.loop(seconds=TODO_TICK_SECONDS)
    async def registration_loop(self) -> None:
        try:
            due = await self.database.claim_due_registrations()
        except psycopg.Error:
            logger.exception("Failed to claim due TodoTodo registrations")
            return
        for row in due:
            try:
                await self._register_todo(row)
            except Exception:  # keep the loop alive on unexpected errors
                logger.exception(
                    "Unexpected error while registering TodoTodo item for %s",
                    row.get("channel_id"),
                )

    @registration_loop.before_loop
    async def before_registration_loop(self) -> None:
        await self.client.wait_until_ready()
        try:
            await self.todotodo.ensure_assistant_member()
            logger.info(
                "TodoTodo assistant member is ready (name=%s)", ASSISTANT_NAME
            )
        except TodoTodoError as exc:
            logger.warning(
                "Could not join the TodoTodo list yet; will retry lazily: %s", exc
            )

    async def register_channel(self, channel: discord.TextChannel) -> None:
        """Register the TodoTodo item for one ticket channel immediately."""
        inserted = await self.database.register_todo(channel.id)
        if not inserted:
            return
        row = {
            "channel_id": str(channel.id),
            "channel_name": channel.name,
            "guild_id": str(channel.guild.id),
        }
        await self._register_todo(row)

    async def _register_todo(self, row: dict[str, Any]) -> None:
        channel_id = row["channel_id"]
        description = build_todo_description(row["guild_id"], channel_id)
        try:
            if self.todotodo.assistant_user_id is None:
                await self.todotodo.ensure_assistant_member()
            todo_id = await self.todotodo.create_todo(
                build_todo_title(row["channel_name"]), description
            )
        except TodoTodoError as exc:
            await self._record_failure(row, str(exc))
            return
        try:
            await self.database.mark_todo_registered(channel_id, todo_id)
        except psycopg.Error:
            logger.exception(
                "Failed to mark TodoTodo item as registered for %s", channel_id
            )
            return
        logger.info(
            "Registered TodoTodo item: channel_id=%s todo_id=%s",
            channel_id,
            todo_id,
        )

    async def _record_failure(self, row: dict[str, Any], error: str) -> None:
        channel_id = row["channel_id"]
        try:
            status, attempts = await self.database.fail_todo_registration(
                channel_id, error
            )
        except psycopg.Error:
            logger.exception(
                "Failed to record TodoTodo registration failure for %s",
                channel_id,
            )
            return
        logger.error(
            "TodoTodo registration failed: channel_id=%s attempts=%s "
            "status=%s error=%s",
            channel_id,
            attempts,
            status,
            error,
        )
        if status != "error":
            return
        operations_channel_id = self.config.operations_channel_id
        if operations_channel_id is None:
            return
        try:
            channel = await self.client.fetch_channel(operations_channel_id)
            if not isinstance(channel, discord.TextChannel):
                return
            await channel.send(
                embed=build_todo_registration_failure_embed(
                    channel_id,
                    row["guild_id"],
                    attempts,
                    error,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, psycopg.Error):
            logger.exception("Failed to notify operations about TodoTodo failure")


class TodoReminderScheduler:
    """TodoTodo watch loop: deadline digest + new-item announcements.

    Every 60 seconds (while ``RGA_TODO_REMIND_CHANNEL_ID`` is set) it fetches
    the list and (1) once a day at the configured JST time posts a silent
    digest of unfinished todos due tomorrow or overdue, and (2) announces
    silently any todos created on the TodoTodo side (bot-created ones
    excluded). The sent date and the seen todo ids are tracked in memory /
    PostgreSQL respectively; a restart after the digest time can repost it
    once, and the first run after deployment only seeds existing ids.
    """

    def __init__(
        self,
        client: discord.Client,
        todotodo: TodoTodoClient,
        config: Config,
        database: TodoDatabase,
    ) -> None:
        self.client = client
        self.todotodo = todotodo
        self.config = config
        self.database = database
        self._last_sent_date: str | None = None

    def start(self) -> None:
        self.remind_loop.start()

    @tasks.loop(seconds=TODO_REMIND_TICK_SECONDS)
    async def remind_loop(self) -> None:
        if self.config.todo_remind_channel_id is None:
            return
        now = datetime.now(JST)
        try:
            payload = await self.todotodo.list_todos()
        except TodoTodoError as exc:
            logger.warning(
                "Could not fetch the TodoTodo list for the reminder; "
                "will retry: %s",
                exc,
            )
            return
        await self._maybe_send_deadline_digest(payload, now)
        await self._notify_new_todos(payload)

    async def _maybe_send_deadline_digest(
        self, payload: Any, now: datetime
    ) -> None:
        """Send the daily deadline digest at most once per day."""
        target = now.replace(
            hour=self.config.todo_remind_time.hour,
            minute=self.config.todo_remind_time.minute,
            second=0,
            microsecond=0,
        )
        if now < target or self._last_sent_date == now.date().isoformat():
            return
        items = collect_deadline_reminders(payload, now)
        self._last_sent_date = now.date().isoformat()
        if not items:
            return
        embed = build_todo_remind_embed(items)
        try:
            await self._send_remind(embed)
            logger.info(
                "Sent TodoTodo deadline reminder with %s items", len(items)
            )
        except discord.HTTPException:
            logger.exception("Failed to send the TodoTodo deadline reminder")

    async def _notify_new_todos(self, payload: Any) -> None:
        """Announce todos created on the TodoTodo side since the last check.

        Bot-created todos (the assistant member is their assignee/user) are
        excluded; the first run with an empty seen-table only seeds the
        existing ids without notifying.
        """
        todos = [
            todo
            for todo in (payload or {}).get("todos") or []
            if isinstance(todo, dict) and todo.get("id")
        ]
        if not todos:
            return
        if self.todotodo.assistant_user_id is None:
            try:
                await self.todotodo.ensure_assistant_member()
            except TodoTodoError as exc:
                logger.warning(
                    "Could not resolve the assistant member for the "
                    "new-todo check: %s",
                    exc,
                )
                return
        assistant = self.todotodo.assistant_user_id
        candidates = [
            todo
            for todo in todos
            if todo.get("userId") != assistant
            and todo.get("assigneeId") != assistant
        ]
        all_ids = [todo["id"] for todo in todos]
        try:
            if await self.database.seen_todo_count() == 0:
                # Bootstrap: seed every current id silently.
                await self.database.claim_new_todos(all_ids)
                return
            new_ids = set(await self.database.claim_new_todos(
                [todo["id"] for todo in candidates]
            ))
        except psycopg.Error:
            logger.exception("Failed to track seen TodoTodo items")
            return
        fresh = [todo for todo in candidates if todo["id"] in new_ids]
        if not fresh:
            return
        embed = build_new_todo_embed(fresh)
        try:
            await self._send_remind(embed)
            logger.info("Announced %s new TodoTodo items", len(fresh))
        except discord.HTTPException:
            logger.exception("Failed to announce new TodoTodo items")

    async def _send_remind(self, embed: discord.Embed) -> None:
        """Post a silent (@silent equivalent) reminder to the configured channel."""
        mention = build_ops_mention(self.config)
        allowed_mentions = (
            discord.AllowedMentions.none()
            if mention is None
            else discord.AllowedMentions.all()
        )
        channel = await self.client.fetch_channel(
            self.config.todo_remind_channel_id
        )
        if not isinstance(channel, discord.TextChannel):
            logger.error(
                "TodoTodo reminder channel %s is not a text channel",
                self.config.todo_remind_channel_id,
            )
            return
        await channel.send(
            content=mention,
            embed=embed,
            silent=True,
            allowed_mentions=allowed_mentions,
        )

    @remind_loop.before_loop
    async def before_remind_loop(self) -> None:
        await self.client.wait_until_ready()
