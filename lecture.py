import logging
from datetime import datetime, timedelta, timezone

import discord
import psycopg
from discord import app_commands
from discord.ext import tasks

from config import Config
from database import LectureDatabase
from embeds import (
    COLOR_CANCELLED,
    COLOR_INFO,
    COLOR_REMINDER,
    MAX_DESCRIPTION,
    MAX_FIELD_VALUE,
    error_embed,
    success_embed,
    truncate,
)

JST = timezone(timedelta(hours=9), name="JST")
logger = logging.getLogger("rga_assistant")

START_TIME_FORMAT = "%Y/%m/%d %H:%M"
LECTURE_TICK_SECONDS = 30
LECTURE_RETRY_SECONDS = 60.0


def parse_start_time(value: str) -> datetime:
    """Parse a JST start time such as ``2026/10/18 15:00`` into an aware UTC datetime."""
    try:
        parsed = datetime.strptime(value.strip(), START_TIME_FORMAT)
    except ValueError as exc:
        raise ValueError(
            "開始日時は `YYYY/MM/DD HH:MM`（日本時間）の形式で指定してください。"
            "例: 2026/10/18 15:00"
        ) from exc
    return parsed.replace(tzinfo=JST).astimezone(timezone.utc)


def reminder_due_at(starts_at: datetime, lead_minutes: int) -> datetime:
    return starts_at.astimezone(timezone.utc) - timedelta(minutes=lead_minutes)


def is_past(starts_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return starts_at.astimezone(timezone.utc) <= now


def has_lecture_permission(
    is_administrator: bool,
    member_role_ids: set[int],
    required_role_ids: tuple[int, ...],
) -> bool:
    if is_administrator:
        return True
    if not required_role_ids:
        return False
    return bool(member_role_ids.intersection(required_role_ids))


def _format_jst(moment: datetime) -> str:
    return moment.astimezone(JST).strftime("%Y/%m/%d %H:%M")


def _escape(value: str) -> str:
    return discord.utils.escape_markdown(value)


def build_announcement_embed(
    title: str,
    lecturer: str,
    description: str,
    starts_at: datetime,
    lead_minutes: int,
    message_link: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title="講義案内",
        description=f"**{_escape(title)}**",
        color=COLOR_INFO,
    )
    embed.add_field(name="講師", value=_escape(lecturer) or "-", inline=True)
    embed.add_field(name="日時", value=f"{_format_jst(starts_at)} JST", inline=True)
    if description:
        embed.add_field(
            name="説明", value=truncate(_escape(description), MAX_FIELD_VALUE), inline=False
        )
    if message_link:
        embed.add_field(name="講義リンク", value=message_link, inline=False)
    embed.set_footer(text=f"開始の{lead_minutes}分前にこのチャンネルでリマインダーを送信します。")
    return embed


def build_reminder_embed(
    title: str,
    lecturer: str,
    starts_at: datetime,
    lead_minutes: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="講義開始リマインダー",
        description=f"講義 **{_escape(title)}** が{lead_minutes}分後に開始します。",
        color=COLOR_REMINDER,
    )
    embed.add_field(name="講師", value=_escape(lecturer) or "-", inline=True)
    embed.add_field(name="日時", value=f"{_format_jst(starts_at)} JST", inline=True)
    return embed


def build_change_notice_embed(
    title: str,
    lecturer: str,
    starts_at: datetime,
) -> discord.Embed:
    embed = discord.Embed(
        title="講義情報の変更",
        description=(
            f"講義 **{_escape(title)}** の内容が変更されました。"
            "最新の情報を確認してください。"
        ),
        color=COLOR_INFO,
    )
    embed.add_field(name="講師", value=_escape(lecturer) or "-", inline=True)
    embed.add_field(name="日時", value=f"{_format_jst(starts_at)} JST", inline=True)
    return embed


def build_cancellation_embed(title: str, starts_at: datetime) -> discord.Embed:
    return discord.Embed(
        title="講義の中止",
        description=(
            f"講義 **{_escape(title)}**（{_format_jst(starts_at)} JST）は中止になりました。"
        ),
        color=COLOR_CANCELLED,
    )


def build_lecture_list_embed(
    lectures: list[dict],
    now: datetime | None = None,
) -> discord.Embed:
    if not lectures:
        return discord.Embed(
            title="講義予定一覧",
            description="登録されている講義予定はありません。",
            color=COLOR_INFO,
        )
    now = now or datetime.now(timezone.utc)
    lines = []
    for lecture in lectures:
        starts_at = lecture["starts_at"]
        if lecture["status"] == "cancelled":
            state = "中止"
        elif is_past(starts_at, now):
            state = "終了"
        else:
            state = "予定"
        lines.append(
            f"- `{lecture['lecture_id']}` **{_escape(lecture['title'])}**"
            f" — {_format_jst(starts_at)} JST"
            f"（講師：{_escape(lecture['lecturer'])}）[{state}]"
        )
    embed = discord.Embed(
        title="講義予定一覧",
        description=truncate("\n".join(lines), MAX_DESCRIPTION),
        color=COLOR_INFO,
    )
    embed.set_footer(text="IDは `/lecture edit` と `/lecture cancel` で使用します。")
    return embed


class LectureCommands(app_commands.Group):
    """Slash commands under ``/lecture``."""

    def __init__(self, config: Config, database: LectureDatabase) -> None:
        super().__init__(
            name="lecture",
            description="講義予定の登録・変更・中止・一覧を行います。",
        )
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
                "講義予定を操作する権限がありません。運営に確認してください。"
            ),
            ephemeral=True,
        )
        return False

    async def _resolve_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None
    ) -> discord.TextChannel | None:
        if channel is not None:
            return channel
        channel_id = self.config.lecture_announce_channel_id
        if channel_id is None:
            return None
        resolved = interaction.client.get_channel(channel_id)
        if resolved is None:
            try:
                resolved = await interaction.client.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return resolved if isinstance(resolved, discord.TextChannel) else None

    async def _edit_message(
        self,
        interaction: discord.Interaction,
        channel_id: str | None,
        message_id: str | None,
        embed: discord.Embed,
    ) -> None:
        if not channel_id or not message_id:
            return
        try:
            channel = await interaction.client.fetch_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                return
            message = await channel.fetch_message(int(message_id))
            await message.edit(
                content=None,
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            logger.exception(
                "Failed to edit lecture message: channel_id=%s message_id=%s",
                channel_id,
                message_id,
            )

    @app_commands.command(name="create", description="講義予定を登録します。")
    @app_commands.describe(
        title="講義名",
        start="開始日時（日本時間、例: 2026/10/18 15:00）",
        lecturer="講師名",
        description="講義の説明（任意）",
        channel="案内を投稿するチャンネル（未指定なら設定値を使用）",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        title: str,
        start: str,
        lecturer: str,
        description: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._ensure_permission(interaction):
            return

        try:
            starts_at = parse_start_time(start)
        except ValueError as exc:
            await interaction.response.send_message(
                embed=error_embed(str(exc)), ephemeral=True
            )
            return
        if is_past(starts_at):
            await interaction.response.send_message(
                embed=error_embed("開始日時は未来の日時を指定してください。"),
                ephemeral=True,
            )
            return

        target = await self._resolve_channel(interaction, channel)
        if target is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "投稿先チャンネルが見つかりません。チャンネルを指定するか、"
                    "`RGA_LECTURE_ANNOUNCE_CHANNEL_ID` を設定してください。"
                ),
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("このコマンドはサーバー内でのみ実行できます。"),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        lecture = await self.database.create(
            interaction.guild.id,
            title=title.strip(),
            lecturer=lecturer.strip(),
            description=(description or "").strip(),
            starts_at=starts_at,
            announce_channel_id=target.id,
            reminder_lead_minutes=self.config.lecture_reminder_minutes,
            created_by=interaction.user.id,
        )
        try:
            message = await target.send(
                embed=build_announcement_embed(
                    lecture["title"],
                    lecture["lecturer"],
                    lecture["description"],
                    starts_at,
                    self.config.lecture_reminder_minutes,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            await self.database.delete(lecture["lecture_id"])
            logger.exception("Failed to post lecture announcement")
            await interaction.followup.send(
                embed=error_embed(f"講義案内の投稿に失敗しました: {exc}"), ephemeral=True
            )
            return
        await self.database.set_announcement_message(lecture["lecture_id"], message.id)
        logger.info(
            "Registered lecture: lecture_id=%s starts_at=%s channel_id=%s",
            lecture["lecture_id"],
            starts_at.isoformat(),
            target.id,
        )
        embed = success_embed(
            "講義を登録しました",
            f"**{_escape(lecture['title'])}** の案内を投稿しました。",
        )
        embed.add_field(name="講義ID", value=f"`{lecture['lecture_id']}`", inline=True)
        embed.add_field(
            name="開始", value=f"{_format_jst(starts_at)} JST", inline=True
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="edit", description="講義予定を変更します。")
    @app_commands.describe(
        lecture_id="変更する講義のID（`/lecture list` で確認できます）",
        title="新しい講義名",
        start="新しい開始日時（日本時間、例: 2026/10/18 15:00）",
        lecturer="新しい講師名",
        description="新しい説明",
        channel="案内を投稿し直すチャンネル",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        lecture_id: str,
        title: str | None = None,
        start: str | None = None,
        lecturer: str | None = None,
        description: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._ensure_permission(interaction):
            return

        lecture = await self.database.get(lecture_id.strip())
        if lecture is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "指定した講義が見つかりません。`/lecture list` でIDを確認してください。"
                ),
                ephemeral=True,
            )
            return
        if lecture["status"] != "scheduled":
            await interaction.response.send_message(
                embed=error_embed("中止済みの講義は変更できません。"), ephemeral=True
            )
            return
        if title is None and start is None and lecturer is None and description is None and channel is None:
            await interaction.response.send_message(
                embed=error_embed("変更する項目を1つ以上指定してください。"),
                ephemeral=True,
            )
            return

        starts_at = None
        if start is not None:
            try:
                starts_at = parse_start_time(start)
            except ValueError as exc:
                await interaction.response.send_message(
                    embed=error_embed(str(exc)), ephemeral=True
                )
                return
            if is_past(starts_at):
                await interaction.response.send_message(
                    embed=error_embed("新しい開始日時は未来の日時を指定してください。"),
                    ephemeral=True,
                )
                return

        target = None
        if channel is not None:
            target = await self._resolve_channel(interaction, channel)
            if target is None:
                await interaction.response.send_message(
                    embed=error_embed("投稿先チャンネルが見つかりません。"),
                    ephemeral=True,
                )
                return

        await interaction.response.defer(thinking=True)
        updated = await self.database.update(
            lecture_id.strip(),
            title=None if title is None else title.strip(),
            lecturer=None if lecturer is None else lecturer.strip(),
            description=None if description is None else description.strip(),
            starts_at=starts_at,
            announce_channel_id=None if target is None else target.id,
        )
        if updated is None:
            await interaction.followup.send(
                embed=error_embed("講義が見つからなかったため変更できませんでした。"),
                ephemeral=True,
            )
            return

        # Edit the messages where they were originally posted, then post the change
        # notice to the (possibly new) announcement channel.
        original_channel_id = lecture["announce_channel_id"]
        new_starts_at = updated["starts_at"]
        notice_channel_id = target.id if target is not None else int(original_channel_id)

        await self._edit_message(
            interaction,
            original_channel_id,
            updated["announcement_message_id"],
            build_announcement_embed(
                updated["title"],
                updated["lecturer"],
                updated["description"],
                new_starts_at,
                updated["reminder_lead_minutes"],
            ),
        )
        if updated["reminder_message_id"]:
            await self._edit_message(
                interaction,
                original_channel_id,
                updated["reminder_message_id"],
                build_reminder_embed(
                    updated["title"],
                    updated["lecturer"],
                    new_starts_at,
                    updated["reminder_lead_minutes"],
                ),
            )

        try:
            channel = await interaction.client.fetch_channel(notice_channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    embed=build_change_notice_embed(
                        updated["title"], updated["lecturer"], new_starts_at
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to post lecture change notice for %s", lecture_id)

        logger.info("Updated lecture: lecture_id=%s", lecture_id)
        embed = success_embed(
            "講義を変更しました",
            f"**{_escape(updated['title'])}** の案内を更新し、変更通知を投稿しました。",
        )
        embed.add_field(name="講義ID", value=f"`{updated['lecture_id']}`", inline=True)
        embed.add_field(
            name="開始", value=f"{_format_jst(new_starts_at)} JST", inline=True
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="cancel", description="講義予定を中止します。")
    @app_commands.describe(lecture_id="中止する講義のID（`/lecture list` で確認できます）")
    async def cancel(
        self,
        interaction: discord.Interaction,
        lecture_id: str,
    ) -> None:
        if not await self._ensure_permission(interaction):
            return

        lecture = await self.database.get(lecture_id.strip())
        if lecture is None:
            await interaction.response.send_message(
                embed=error_embed(
                    "指定した講義が見つかりません。`/lecture list` でIDを確認してください。"
                ),
                ephemeral=True,
            )
            return
        if lecture["status"] == "cancelled":
            await interaction.response.send_message(
                embed=error_embed("この講義はすでに中止されています。"), ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        cancelled = await self.database.cancel(lecture_id.strip())
        if cancelled is None:
            await interaction.followup.send(
                embed=error_embed("講義が見つからなかったため中止できませんでした。"),
                ephemeral=True,
            )
            return
        starts_at = cancelled["starts_at"]
        cancellation = build_cancellation_embed(cancelled["title"], starts_at)

        if cancelled["announcement_message_id"]:
            await self._edit_message(
                interaction,
                cancelled["announce_channel_id"],
                cancelled["announcement_message_id"],
                cancellation,
            )
        if cancelled["reminder_message_id"]:
            await self._edit_message(
                interaction,
                cancelled["announce_channel_id"],
                cancelled["reminder_message_id"],
                cancellation,
            )

        try:
            channel = await interaction.client.fetch_channel(
                int(cancelled["announce_channel_id"])
            )
            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    embed=cancellation,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to post lecture cancellation for %s", lecture_id)

        logger.info("Cancelled lecture: lecture_id=%s", lecture_id)
        embed = success_embed(
            "講義を中止しました",
            f"**{_escape(cancelled['title'])}** の案内を中止表示に更新しました。",
        )
        embed.add_field(name="講義ID", value=f"`{cancelled['lecture_id']}`", inline=True)
        embed.add_field(
            name="開始", value=f"{_format_jst(starts_at)} JST", inline=True
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="list", description="講義予定の一覧を表示します。")
    @app_commands.describe(include_cancelled="中止した講義も表示します。")
    async def list_lectures(
        self,
        interaction: discord.Interaction,
        include_cancelled: bool = False,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("このコマンドはサーバー内でのみ実行できます。"),
                ephemeral=True,
            )
            return
        lectures = await self.database.list_lectures(
            interaction.guild.id, include_cancelled=include_cancelled
        )
        await interaction.response.send_message(
            embed=build_lecture_list_embed(lectures),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class LectureScheduler:
    """Sends lecture reminders that are due, persisting state in SQLite."""

    def __init__(
        self,
        client: discord.Client,
        config: Config,
        database: LectureDatabase,
    ) -> None:
        self.client = client
        self.config = config
        self.database = database

    def start(self) -> None:
        self.reminder_loop.start()

    @tasks.loop(seconds=LECTURE_TICK_SECONDS)
    async def reminder_loop(self) -> None:
        try:
            due = await self.database.claim_due()
        except psycopg.Error:
            logger.exception("Failed to claim due lecture reminders")
            return
        for lecture in due:
            try:
                await self._send_reminder(lecture)
            except Exception:  # keep the loop alive on unexpected errors
                logger.exception(
                    "Unexpected error while sending lecture reminder %s",
                    lecture.get("lecture_id"),
                )

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.client.wait_until_ready()

    async def _send_reminder(self, lecture: dict) -> None:
        lecture_id = lecture["lecture_id"]
        channel_id = int(lecture["announce_channel_id"])
        try:
            channel = await self.client.fetch_channel(channel_id)
        except discord.NotFound:
            await self._expire(lecture_id, "announce channel not found")
            return
        except discord.Forbidden:
            await self._expire(lecture_id, "missing access to announce channel")
            return
        except discord.HTTPException as exc:
            await self._release(lecture_id, f"HTTPException: {exc}")
            return
        except psycopg.Error:
            logger.exception("Failed to check lecture channel %s", channel_id)
            return

        if not isinstance(channel, discord.TextChannel):
            await self._expire(lecture_id, "announce channel is not a text channel")
            return

        starts_at = lecture["starts_at"]
        embed = build_reminder_embed(
            lecture["title"],
            lecture["lecturer"],
            starts_at,
            lecture["reminder_lead_minutes"],
        )
        try:
            sent = await channel.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.Forbidden:
            await self._expire(lecture_id, "missing permission to send message")
            return
        except discord.HTTPException as exc:
            await self._release(lecture_id, f"HTTPException: {exc}")
            return
        except psycopg.Error:
            logger.exception("Failed to send lecture reminder %s", lecture_id)
            return

        try:
            await self.database.mark_reminder_sent(lecture_id, sent.id)
        except psycopg.Error:
            logger.exception("Failed to finalize lecture reminder %s", lecture_id)
            return
        logger.info(
            "Sent lecture reminder: lecture_id=%s message_id=%s",
            lecture_id,
            sent.id,
        )

    async def _release(self, lecture_id: str, error: str) -> None:
        logger.error("Lecture reminder failed: lecture_id=%s error=%s", lecture_id, error)
        try:
            await self.database.release_reminder(
                lecture_id, error, retry_seconds=LECTURE_RETRY_SECONDS
            )
        except psycopg.Error:
            logger.exception("Failed to record lecture reminder failure %s", lecture_id)

    async def _expire(self, lecture_id: str, error: str) -> None:
        logger.error(
            "Lecture reminder gave up: lecture_id=%s error=%s", lecture_id, error
        )
        try:
            await self.database.expire_reminder(lecture_id, error)
        except psycopg.Error:
            logger.exception("Failed to expire lecture reminder %s", lecture_id)
