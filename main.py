import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import discord
import psycopg
from discord import app_commands
from discord.ext import tasks

from config import Config, load_config
from database import (
    LectureDatabase,
    ReminderDatabase,
    TodoDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)
from embeds import (
    COLOR_ERROR,
    COLOR_INFO,
    COLOR_REMINDER,
    MAX_DESCRIPTION,
    MAX_FIELD_VALUE,
    error_embed,
    success_embed,
    truncate,
)
from jev import (
    JevError,
    assessment_to_status,
    build_channel_state,
    evaluate_application_status,
)
from lecture import LectureCommands, LectureScheduler, has_lecture_permission
from todotodo import (
    TodoCommands,
    TodoRegistrationScheduler,
    TodoTodoClient,
)

JST = timezone(timedelta(hours=9), name="JST")
logger = logging.getLogger("rga_assistant")


def is_monitored_channel(
    category_id: int | None,
    channel_id: int,
    category_ids: tuple[int, ...],
    excluded_channel_ids: tuple[int, ...],
) -> bool:
    return category_id in category_ids and channel_id not in excluded_channel_ids


def message_has_image_attachment(
    attachment_content_types: tuple[str | None, ...],
) -> bool:
    """Return True when any attachment is an image file (MIME type image/*)."""
    return any(
        content_type is not None and content_type.startswith("image/")
        for content_type in attachment_content_types
    )


def is_reminder_cancel_message(
    attachment_content_types: tuple[str | None, ...],
    content: str,
    channel_category_id: int | None,
    configured_category_ids: tuple[int, ...],
    channel_id: int,
    excluded_channel_ids: tuple[int, ...],
    cancel_trigger: str,
    trigger_texts: tuple[str, ...],
) -> bool:
    if not is_monitored_channel(
        channel_category_id,
        channel_id,
        configured_category_ids,
        excluded_channel_ids,
    ):
        return False
    if cancel_trigger == "image":
        return message_has_image_attachment(attachment_content_types)
    return content in trigger_texts


def elapsed_days(opened_at: datetime, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return (now - opened_at.astimezone(timezone.utc)).days


def build_ticket_reminder_embed(
    channel_name: str,
    opened_at: datetime,
    days: int,
    guild_id: str,
    channel_id: str,
    cancel_trigger: str,
) -> discord.Embed:
    opened_at_jst = opened_at.astimezone(JST)
    if cancel_trigger == "image":
        cancel_hint = "申請画像が読み取れなかった場合があります。"
    else:
        cancel_hint = "「審査をお願いします。」という文章が読み取れなかった場合があります。"
    embed = discord.Embed(
        title="チケット確認リマインダー",
        description=(
            f"チケット **{discord.utils.escape_markdown(channel_name)}** "
            f"の開設から{days}日が経過しました。\n"
            "もし申請をしているのにも関わらず、この文章が送られた場合は、"
            f"{cancel_hint}"
            "お問い合わせください。"
            "申請者は、もし申請を取り下げる場合、Endというボタンを押してください。"
        ),
        color=COLOR_REMINDER,
    )
    embed.add_field(
        name="開設日時", value=f"{opened_at_jst:%Y/%m/%d %H:%M} JST", inline=False
    )
    embed.add_field(
        name="チケット",
        value=f"https://discord.com/channels/{guild_id}/{channel_id}",
        inline=False,
    )
    return embed


def build_operations_reminder_embed(
    channel_name: str,
    opened_at: datetime,
    days: int,
    guild_id: str,
    channel_id: str,
    application_status_line: str | None = None,
) -> discord.Embed:
    opened_at_jst = opened_at.astimezone(JST)
    embed = discord.Embed(
        title="チケット確認リマインダー",
        description=(
            f"チケット **{discord.utils.escape_markdown(channel_name)}** "
            f"の開設から{days}日が経過しました。\n\n"
            "対応状況を確認してください。"
        ),
        color=COLOR_REMINDER,
    )
    embed.add_field(
        name="開設日時", value=f"{opened_at_jst:%Y/%m/%d %H:%M} JST", inline=False
    )
    embed.add_field(
        name="チケット",
        value=f"https://discord.com/channels/{guild_id}/{channel_id}",
        inline=False,
    )
    if application_status_line is not None:
        embed.add_field(
            name="申請の確認", value=truncate(
                application_status_line, MAX_FIELD_VALUE
            ),
            inline=False,
        )
    return embed


def format_application_status_line(
    status: dict[str, Any] | None,
    confidence_threshold: float,
) -> str:
    """Format the stored application status for the operations reminder field."""
    if not status or status.get("has_application") is None:
        return "未判定（まだ申請の投稿を確認していません）"
    has_application = bool(status["has_application"])
    has_text = "true" if has_application else "false"
    method = status.get("method") or "simple"
    if method in ("jev", "jev_fallback"):
        detail = status.get("detail") or "判断不能"
        confidence = status.get("confidence")
        confidence_text = (
            f"、確信度 {confidence:.2f}"
            if isinstance(confidence, (int, float))
            else ""
        )
        suffix = ""
        if method == "jev_fallback":
            suffix = " — Jevが利用できなかったため簡易判定"
        elif (
            isinstance(confidence, (int, float))
            and confidence < confidence_threshold
        ):
            suffix = " — 要人確認"
        return f"{has_text}（jev: {detail}{confidence_text}）{suffix}"
    return f"{has_text}（simple）"



def build_mention(role_id: int | None, user_id: int | None) -> str | None:
    if role_id is not None:
        return f"<@&{role_id}>"
    if user_id is not None:
        return f"<@{user_id}>"
    return None


def _mention_rules(role_id: int | None, user_id: int | None) -> discord.AllowedMentions:
    if role_id is not None:
        return discord.AllowedMentions(roles=[discord.Object(id=role_id)])
    if user_id is not None:
        return discord.AllowedMentions(users=[discord.Object(id=user_id)])
    return discord.AllowedMentions.none()


def build_delivery_message(
    delivery: dict[str, Any],
    config: Config,
    application_status: dict[str, Any] | None = None,
) -> tuple[str | None, discord.Embed, discord.AllowedMentions]:
    """Build the content, embed, and mention rules for one reminder delivery."""
    opened_at = delivery["opened_at"]
    days = elapsed_days(opened_at)
    if delivery["target"] == "operations":
        role_id = config.operations_mention_role_id
        user_id = config.operations_mention_user_id
        return (
            build_mention(role_id, user_id),
            build_operations_reminder_embed(
                delivery["channel_name"],
                opened_at,
                days,
                delivery["guild_id"],
                delivery["channel_id"],
                application_status_line=format_application_status_line(
                    application_status, config.jev_confidence_threshold
                ),
            ),
            _mention_rules(role_id, user_id),
        )
    return (
        None,
        build_ticket_reminder_embed(
            delivery["channel_name"],
            opened_at,
            days,
            delivery["guild_id"],
            delivery["channel_id"],
            config.cancel_trigger,
        ),
        discord.AllowedMentions.none(),
    )


def build_failure_notification_embed(
    guild_id: str,
    channel_id: str,
    target: str,
    attempts: int,
    error: str,
) -> discord.Embed:
    embed = discord.Embed(title="チケットリマインダー送信失敗", color=COLOR_ERROR)
    embed.add_field(
        name="チャンネル",
        value=f"https://discord.com/channels/{guild_id}/{channel_id}",
        inline=False,
    )
    embed.add_field(name="送信先", value=target, inline=True)
    embed.add_field(name="試行回数", value=str(attempts), inline=True)
    embed.add_field(
        name="理由", value=truncate(error, MAX_FIELD_VALUE) or "-", inline=False
    )
    embed.set_footer(text="`python -m tools.retry` で再試行対象に戻せます。")
    return embed


def build_application_image_notification_embed(
    channel_name: str,
    guild_id: str,
    channel_id: str,
    image_url: str,
) -> discord.Embed:
    embed = discord.Embed(
        title="申請画像を検知しました",
        description=(
            f"チケット **{discord.utils.escape_markdown(channel_name)}** に"
            "申請画像が投稿されました。"
        ),
        color=COLOR_INFO,
    )
    embed.add_field(
        name="チケット",
        value=f"https://discord.com/channels/{guild_id}/{channel_id}",
        inline=False,
    )
    embed.set_image(url=image_url)
    return embed


def build_failure_list_embed(rows: list[dict[str, Any]]) -> discord.Embed:
    if not rows:
        return discord.Embed(
            title="チケットリマインダー送信失敗一覧",
            description="失敗状態の送信はありません。",
            color=COLOR_INFO,
        )
    lines = []
    for row in rows:
        error = truncate(row["last_error"] or "-", 200)
        lines.append(
            f"- チャンネル `{row['channel_id']}`（{row['target']}）"
            f" 試行{row['attempts']}回: {error}"
        )
    embed = discord.Embed(
        title="チケットリマインダー送信失敗一覧",
        description=truncate("\n".join(lines), MAX_DESCRIPTION),
        color=COLOR_ERROR,
    )
    embed.set_footer(text="`/rga retry` で再試行対象に戻せます。")
    return embed


def build_help_embed(cancel_trigger: str) -> discord.Embed:
    embed = discord.Embed(
        title="RGA Assistant の機能",
        description="このBotで利用できる機能とコマンドの一覧です。",
        color=COLOR_INFO,
    )
    cancel_line = (
        "画像が添付された投稿があると、未送信のリマインダーを取り消します。"
        if cancel_trigger == "image"
        else "キャンセル用のキーワードに完全一致する投稿があると、"
        "未送信のリマインダーを取り消します。"
    )
    embed.add_field(
        name="チケットリマインダー",
        value=(
            "監視対象カテゴリでチャンネルが作成されると、自動でリマインダーを"
            "登録します。\n"
            "設定した時間が経過すると、チケットチャンネルまたは運営用チャンネルへ"
            f"通知します。\n{cancel_line}"
        ),
        inline=False,
    )
    embed.add_field(
        name="講義スケジュール",
        value=(
            "`/lecture create` 講義予定を登録します（講師・運営）。\n"
            "`/lecture edit` 講義予定を変更します（講師・運営）。\n"
            "`/lecture cancel` 講義予定を中止します（講師・運営）。\n"
            "`/lecture list` 講義予定の一覧を表示します。\n"
            "講義の開始前に、登録したチャンネルへリマインダーを送信します。"
        ),
        inline=False,
    )
    embed.add_field(
        name="運営向けコマンド",
        value=(
            "`/rga failures` 送信に失敗したチケットリマインダーを一覧表示します"
            "（管理者・運営）。\n"
            "`/rga retry` 失敗した送信を再試行対象に戻します（管理者・運営）。"
        ),
        inline=False,
    )
    embed.add_field(
        name="トドTODO連携",
        value=(
            "`/todo decide` チケットのTODOを決着させます（承認／却下を選択、"
            "運営のみ）。\n"
            "申請画像を検知すると、トドTODOへTODOを自動登録します。"
        ),
        inline=False,
    )
    embed.add_field(
        name="その他",
        value="`/rga help` このヘルプを表示します。",
        inline=False,
    )
    embed.set_footer(text="わからないことがあれば運営までお問い合わせください。")
    return embed


class RGACommands(app_commands.Group):
    def __init__(self, config: Config, database: ReminderDatabase) -> None:
        super().__init__(name="rga", description="RGA Assistant の情報を表示します。")
        self.config = config
        self.database = database

    async def _ensure_permission(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                embed=error_embed("このコマンドはサーバー内でのみ実行できます。"),
                ephemeral=True,
            )
            return False
        if has_lecture_permission(
            member.guild_permissions.administrator,
            {role.id for role in member.roles},
            self.config.lecture_role_ids,
        ):
            return True
        await interaction.response.send_message(
            embed=error_embed(
                "このコマンドを実行する権限がありません。運営に確認してください。"
            ),
            ephemeral=True,
        )
        return False

    @app_commands.command(name="help", description="RGA Assistant の機能とコマンドを表示します。")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=build_help_embed(self.config.cancel_trigger),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="failures",
        description="送信に失敗したチケットリマインダーを一覧表示します。",
    )
    async def failures(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_permission(interaction):
            return
        try:
            rows = await self.database.list_errors()
        except psycopg.Error:
            logger.exception("Failed to list reminder errors")
            await interaction.response.send_message(
                embed=error_embed("失敗一覧の取得に失敗しました。ログを確認してください。"),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=build_failure_list_embed(rows),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(
        name="retry",
        description="失敗したチケットリマインダーを再試行対象に戻します。",
    )
    @app_commands.describe(
        channel_id="対象のチケットチャンネルID（省略すると全ての失敗）。"
    )
    async def retry(
        self,
        interaction: discord.Interaction,
        channel_id: str | None = None,
    ) -> None:
        if not await self._ensure_permission(interaction):
            return
        target_id = None
        if channel_id:
            try:
                target_id = int(channel_id.strip())
            except ValueError:
                await interaction.response.send_message(
                    embed=error_embed("チャンネルIDは数字で指定してください。"),
                    ephemeral=True,
                )
                return
        try:
            count = await self.database.retry_errors(target_id)
        except psycopg.Error:
            logger.exception("Failed to reset reminder errors")
            await interaction.response.send_message(
                embed=error_embed(
                    "再試行対象への更新に失敗しました。ログを確認してください。"
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=success_embed(
                "再試行対象に戻しました",
                f"{count}件の送信を再試行対象に戻しました。",
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RGAAssistant(discord.Client):
    def __init__(self, config: Config) -> None:
        super().__init__(
            intents=discord.Intents(guilds=True, messages=True, message_content=True)
        )
        self.config = config
        self.pool = None
        self.database: ReminderDatabase | None = None
        self.lecture_database: LectureDatabase | None = None
        self.tree = app_commands.CommandTree(self)
        self.lecture_scheduler: LectureScheduler | None = None
        self.jev_session: aiohttp.ClientSession | None = None
        self.todo_session: aiohttp.ClientSession | None = None
        self.todo_client: TodoTodoClient | None = None
        self.todo_database: TodoDatabase | None = None
        self.todo_scheduler: TodoRegistrationScheduler | None = None

    async def setup_hook(self) -> None:
        assert self.pool is None
        if self.config.application_status_mode == "jev":
            self.jev_session = aiohttp.ClientSession()
        self.pool = await open_pool(self.config.database_url)
        self.database = ReminderDatabase(
            self.pool,
            self.config.reminder_after,
            self.config.reminder_targets,
            retry_base_seconds=self.config.retry_base_seconds,
            retry_max_seconds=self.config.retry_max_seconds,
            retry_max_attempts=self.config.retry_max_attempts,
        )
        self.lecture_database = LectureDatabase(self.pool)
        await self.database.initialize()
        await self.lecture_database.initialize()
        self.lecture_scheduler = LectureScheduler(
            self, self.config, self.lecture_database
        )
        self.tree.add_command(LectureCommands(self.config, self.lecture_database))
        self.tree.add_command(RGACommands(self.config, self.database))
        if self.config.todo_base_url and self.config.todo_list_token:
            self.todo_session = aiohttp.ClientSession()
            self.todo_client = TodoTodoClient(
                self.todo_session,
                self.config.todo_base_url,
                self.config.todo_list_token,
            )
            self.todo_database = TodoDatabase(
                self.pool,
                retry_base_seconds=self.config.retry_base_seconds,
                retry_max_seconds=self.config.retry_max_seconds,
                retry_max_attempts=self.config.retry_max_attempts,
            )
            await self.todo_database.initialize()
            self.todo_scheduler = TodoRegistrationScheduler(
                self, self.config, self.todo_database, self.todo_client
            )
            self.todo_scheduler.start()
        self.tree.add_command(
            TodoCommands(self.config, self.todo_database, self.todo_client)
        )
        self.reminder_loop.start()
        self.lecture_scheduler.start()
        try:
            synced = await self.tree.sync()
            logger.info("Synced %s application commands", len(synced))
        except discord.HTTPException:
            logger.exception("Failed to sync application commands")

    async def close(self) -> None:
        logger.info("Shutting down RGA Assistant")
        if self.jev_session is not None:
            await self.jev_session.close()
            self.jev_session = None
        if self.todo_session is not None:
            await self.todo_session.close()
            self.todo_session = None
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Connected to Discord as %s", self.user)
        logger.info(
            "Monitoring category IDs %s; excluded channel IDs %s",
            self.config.ticket_category_ids,
            self.config.excluded_channel_ids,
        )
        logger.info(
            "Reminder targets: %s; operations channel: %s",
            self.config.reminder_targets,
            self.config.operations_channel_id,
        )
        logger.info(
            "Lecture features: announce channel=%s; reminder lead=%s minutes; "
            "manage roles=%s",
            self.config.lecture_announce_channel_id,
            self.config.lecture_reminder_minutes,
            self.config.lecture_role_ids,
        )
        logger.info(
            "TodoTodo integration: %s; decide roles=%s",
            "enabled" if self.todo_client is not None else "disabled",
            self.config.todo_role_ids,
        )
        scanned = 0
        for guild in self.guilds:
            for channel in guild.text_channels:
                if self._is_monitored_ticket_channel(channel):
                    scanned += 1
                    await self._safe_register_ticket(channel)
        logger.info(
            "Startup scan found %s text channels in monitored categories",
            scanned,
        )

    async def on_guild_channel_create(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        if not isinstance(channel, discord.TextChannel):
            logger.debug(
                "Ignoring created non-text channel %s (type=%s)",
                channel.id,
                channel.type,
            )
            return
        logger.info(
            "Observed channel creation: id=%s category_id=%s name=%s",
            channel.id,
            channel.category_id,
            channel.name,
        )
        if self._is_monitored_ticket_channel(channel):
            await self._safe_register_ticket(channel)
        else:
            self._log_ignored_channel(channel)

    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        if not isinstance(after, discord.TextChannel):
            return
        was_monitored = isinstance(before, discord.TextChannel) and (
            self._is_monitored_ticket_channel(before)
        )
        if not was_monitored and self._is_monitored_ticket_channel(after):
            logger.info(
                "Channel entered monitored category: id=%s category_id=%s name=%s",
                after.id,
                after.category_id,
                after.name,
            )
            await self._safe_register_ticket(after)

    async def on_message(self, message: discord.Message) -> None:
        if (
            message.author.bot
            or message.webhook_id is not None
            or message.guild is None
            or not isinstance(message.channel, discord.TextChannel)
        ):
            return

        if self._has_image_attachment(message):
            await self._notify_application_image(message)

        if self._matches_trigger(message):
            if await self.database.cancel(message.channel.id):
                logger.info(
                    "Cancelled ticket reminder after %s trigger in channel %s",
                    self.config.cancel_trigger,
                    message.channel.id,
                )

        await self._update_application_status(message)

    async def _update_application_status(self, message: discord.Message) -> None:
        """Record or re-evaluate the application status of a ticket channel."""
        if not self._is_monitored_ticket_channel(message.channel):
            return
        try:
            if self.config.application_status_mode == "jev":
                await self._evaluate_application_with_jev(message)
            else:
                await self._record_simple_application_status(message)
        except Exception:  # status tracking must never break message handling
            logger.exception(
                "Failed to update application status for ticket %s",
                message.channel.id,
            )

    async def _record_simple_application_status(
        self, message: discord.Message
    ) -> None:
        """Track the image-based application criterion without any external API."""
        image_attached = message_has_image_attachment(
            tuple(attachment.content_type for attachment in message.attachments)
        )
        channel_id = str(message.channel.id)
        current = await self.database.get_application_status(channel_id)
        if (
            current is not None
            and current.get("method") == "simple"
            and bool(current.get("has_application")) == image_attached
        ):
            return
        await self.database.set_application_status(
            channel_id, image_attached, "simple"
        )

    async def _collect_channel_state_messages(
        self, channel: discord.TextChannel
    ) -> list[dict[str, Any]]:
        """Collect the recent channel history (oldest first) as plain dicts."""
        messages = []
        async for history_message in channel.history(
            limit=self.config.jev_history_limit
        ):
            messages.append(
                {
                    "author": getattr(
                        history_message.author, "display_name",
                        str(history_message.author),
                    ),
                    "content": history_message.content or "(Embed)",
                    "attachments": [
                        {"content_type": attachment.content_type}
                        for attachment in history_message.attachments
                    ],
                }
            )
        messages.reverse()
        return messages

    async def _evaluate_application_with_jev(
        self, message: discord.Message
    ) -> None:
        """Ask Jev about the channel history, falling back to simple detection."""
        channel_id = str(message.channel.id)
        now = datetime.now(timezone.utc)
        current = await self.database.get_application_status(channel_id)
        if current is not None and current.get("method") == "jev":
            evaluated_at = current.get("updated_at")
            cooldown = timedelta(seconds=self.config.jev_evaluation_cooldown_seconds)
            if (
                isinstance(evaluated_at, datetime)
                and now - evaluated_at.astimezone(timezone.utc) < cooldown
            ):
                return

        state = build_channel_state(await self._collect_channel_state_messages(
            message.channel
        ))
        assert self.jev_session is not None
        try:
            assessment = await evaluate_application_status(
                self.jev_session,
                self.config.jev_api_key or "",
                self.config.jev_model,
                state,
            )
        except JevError as exc:
            # The issue allows falling back to simple detection on Jev failure.
            logger.warning(
                "Jev evaluation failed for ticket %s; falling back to "
                "simple detection: %s",
                message.channel.id,
                exc,
            )
            await self.database.set_application_status(
                channel_id,
                message_has_image_attachment(
                    tuple(
                        attachment.content_type
                        for attachment in message.attachments
                    )
                ),
                "jev_fallback",
            )
            return

        has_application, detail = assessment_to_status(assessment)
        await self.database.set_application_status(
            channel_id=channel_id,
            has_application=has_application,
            method="jev",
            detail=detail,
            confidence=assessment.confidence,
        )
        logger.info(
            "Jev application status for ticket %s: %s (confidence=%s)",
            message.channel.id,
            detail,
            assessment.confidence,
        )

    def _matches_trigger(self, message: discord.Message) -> bool:
        return is_reminder_cancel_message(
            tuple(attachment.content_type for attachment in message.attachments),
            message.content,
            message.channel.category_id,
            self.config.ticket_category_ids,
            message.channel.id,
            self.config.excluded_channel_ids,
            self.config.cancel_trigger,
            self.config.reminder_trigger_texts,
        )

    def _has_image_attachment(self, message: discord.Message) -> bool:
        return self._is_monitored_ticket_channel(
            message.channel
        ) and message_has_image_attachment(
            tuple(attachment.content_type for attachment in message.attachments)
        )

    async def _notify_application_image(self, message: discord.Message) -> None:
        operations_channel_id = self.config.operations_channel_id
        if operations_channel_id is None:
            logger.error(
                "Cannot notify application image for ticket %s: "
                "operations channel is not configured",
                message.channel.id,
            )
            return

        image = next(
            (
                attachment
                for attachment in message.attachments
                if attachment.content_type is not None
                and attachment.content_type.startswith("image/")
            ),
            None,
        )
        if image is None:
            return
        try:
            claimed = await self.database.claim_application_image_notification(
                message.channel.id
            )
        except psycopg.Error:
            logger.exception(
                "Failed to claim application image notification for ticket %s",
                message.channel.id,
            )
            return
        if not claimed:
            return

        await self._register_ticket_todo(message.channel)

        try:
            channel = await self.fetch_channel(operations_channel_id)
            if not isinstance(channel, discord.TextChannel):
                raise TypeError("operations destination is not a text channel")
            await channel.send(
                content=build_mention(
                    self.config.operations_mention_role_id,
                    self.config.operations_mention_user_id,
                ),
                embed=build_application_image_notification_embed(
                    message.channel.name,
                    str(message.guild.id),
                    str(message.channel.id),
                    image.url,
                ),
                allowed_mentions=_mention_rules(
                    self.config.operations_mention_role_id,
                    self.config.operations_mention_user_id,
                ),
            )
        except (discord.HTTPException, TypeError):
            try:
                await self.database.release_application_image_notification(
                    message.channel.id
                )
            except psycopg.Error:
                logger.exception(
                    "Failed to release application image notification claim for %s",
                    message.channel.id,
                )
            logger.exception(
                "Failed to notify operations about application image in ticket %s",
                message.channel.id,
            )
            return

        try:
            await self.database.mark_application_image_notification_sent(
                message.channel.id
            )
        except psycopg.Error:
            logger.exception(
                "Failed to mark application image notification as sent for %s",
                message.channel.id,
            )
            return
        logger.info(
            "Notified operations about application image in ticket %s",
            message.channel.id,
        )

    async def _register_ticket_todo(self, channel: discord.TextChannel) -> None:
        """Register the TodoTodo item once per ticket (Phase 5 hook).

        Failures here must never break the application image notification.
        """
        if self.todo_scheduler is None:
            return
        try:
            await self.todo_scheduler.register_channel(channel)
        except psycopg.Error:
            logger.exception(
                "Failed to register TodoTodo item for ticket %s", channel.id
            )
        except Exception:
            logger.exception(
                "Unexpected error while registering TodoTodo item for ticket %s",
                channel.id,
            )

    def _is_monitored_ticket_channel(self, channel: discord.TextChannel) -> bool:
        return is_monitored_channel(
            channel.category_id,
            channel.id,
            self.config.ticket_category_ids,
            self.config.excluded_channel_ids,
        )

    @staticmethod
    def _log_ignored_channel(channel: discord.TextChannel) -> None:
        logger.info(
            "Channel not monitored: id=%s category_id=%s (category mismatch or excluded)",
            channel.id,
            channel.category_id,
        )

    async def _safe_register_ticket(self, channel: discord.TextChannel) -> None:
        """Register one channel without letting a failure abort a batch scan."""
        try:
            await self._register_ticket(channel)
        except Exception:
            logger.exception(
                "Failed to register reminder for channel %s", channel.id
            )

    async def _register_ticket(self, channel: discord.TextChannel) -> None:
        inserted = await self.database.register(
            channel.id,
            channel.guild.id,
            channel.name,
            channel.created_at,
        )
        if inserted:
            logger.info(
                "Scheduled reminder: channel_id=%s targets=%s created_at=%s due_at=%s",
                channel.id,
                self.config.reminder_targets,
                channel.created_at.isoformat(),
                (channel.created_at + self.config.reminder_after).isoformat(),
            )
        else:
            logger.info(
                "Reminder already exists for channel_id=%s",
                channel.id,
            )

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if await self.database.cancel(channel.id):
            logger.info("Cancelled pending reminder for deleted channel %s", channel.id)

    @tasks.loop(seconds=5)
    async def reminder_loop(self) -> None:
        try:
            due = await self.database.claim_due()
        except psycopg.Error:
            logger.exception("Failed to claim due reminders")
            return
        for delivery in due:
            try:
                await self._send_delivery(delivery)
            except Exception:  # keep the loop alive on unexpected errors
                logger.exception(
                    "Unexpected error while sending reminder for %s",
                    delivery.get("channel_id"),
                )

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.wait_until_ready()

    async def _send_delivery(self, delivery: dict[str, Any]) -> None:
        target = delivery["target"]
        if target == "operations":
            destination_id = self.config.operations_channel_id
            if destination_id is None:
                await self._record_failure(
                    delivery,
                    "operations channel is not configured",
                    permanent=True,
                )
                return
        else:
            destination_id = int(delivery["channel_id"])

        try:
            channel = await self.fetch_channel(destination_id)
        except discord.NotFound:
            if target == "ticket":
                await self.database.cancel(int(delivery["channel_id"]))
                logger.info(
                    "Cancelled reminder for unavailable ticket %s",
                    delivery["channel_id"],
                )
            else:
                await self._record_failure(
                    delivery, "operations channel not found", permanent=True
                )
            return
        except discord.Forbidden:
            await self._record_failure(
                delivery, "missing access to destination channel", permanent=True
            )
            return
        except discord.HTTPException as exc:
            await self._record_failure(delivery, f"HTTPException: {exc}", permanent=False)
            return
        except psycopg.Error:
            logger.exception(
                "Failed to check destination channel %s", delivery["channel_id"]
            )
            return

        if not isinstance(channel, discord.TextChannel):
            if target == "ticket":
                await self.database.cancel(int(delivery["channel_id"]))
                logger.info(
                    "Ticket channel %s is no longer available",
                    delivery["channel_id"],
                )
            else:
                await self._record_failure(
                    delivery, "destination is not a text channel", permanent=True
                )
            return

        if not await self.database.is_sending(delivery["channel_id"], target):
            return

        application_status = None
        if target == "operations":
            try:
                application_status = await self.database.get_application_status(
                    delivery["channel_id"]
                )
            except psycopg.Error:
                logger.exception(
                    "Failed to load application status for ticket %s",
                    delivery["channel_id"],
                )

        content, embed, allowed_mentions = build_delivery_message(
            delivery, self.config, application_status
        )

        try:
            sent_message = await channel.send(
                content=content, embed=embed, allowed_mentions=allowed_mentions
            )
        except discord.Forbidden:
            await self._record_failure(
                delivery, "missing permission to send message", permanent=True
            )
            return
        except discord.HTTPException as exc:
            await self._record_failure(delivery, f"HTTPException: {exc}", permanent=False)
            return
        except psycopg.Error:
            logger.exception(
                "Failed to send %s reminder for ticket %s",
                target,
                delivery["channel_id"],
            )
            return

        await self.database.mark_delivery_sent(delivery["channel_id"], target)
        logger.info(
            "Sent %s reminder: channel_id=%s message_id=%s",
            target,
            delivery["channel_id"],
            sent_message.id,
        )

    async def _record_failure(
        self,
        delivery: dict[str, Any],
        error: str,
        permanent: bool,
    ) -> None:
        try:
            status, attempts = await self.database.fail_delivery(
                delivery["channel_id"], delivery["target"], error, permanent
            )
        except psycopg.Error:
            logger.exception(
                "Failed to record delivery failure for %s", delivery["channel_id"]
            )
            return
        logger.error(
            "Reminder delivery failed: channel_id=%s target=%s attempts=%s "
            "status=%s error=%s",
            delivery["channel_id"],
            delivery["target"],
            attempts,
            status,
            error,
        )
        if status == "error" and delivery["target"] != "operations":
            await self._notify_failure(delivery, attempts, error)

    async def _notify_failure(
        self,
        delivery: dict[str, Any],
        attempts: int,
        error: str,
    ) -> None:
        channel_id = self.config.operations_channel_id
        if channel_id is None:
            return
        try:
            channel = await self.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                return
            await channel.send(
                embed=build_failure_notification_embed(
                    delivery["guild_id"],
                    delivery["channel_id"],
                    delivery["target"],
                    attempts,
                    error,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, psycopg.Error):
            logger.exception("Failed to notify operations about reminder failure")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # psycopg's async connections need a selector loop on Windows; this must run
    # before ``client.run`` creates the event loop.
    ensure_compatible_event_loop_policy()
    try:
        config = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    client = RGAAssistant(config)
    try:
        client.run(config.discord_token, log_handler=None)
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "Discord Developer Portalで Message Content Intent を有効にしてください。"
            "Applications > 対象Bot > Bot > Privileged Gateway Intents から設定できます。"
        )
    except KeyboardInterrupt:
        logger.info("Bot stopped")


if __name__ == "__main__":
    main()
