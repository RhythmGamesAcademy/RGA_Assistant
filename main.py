import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import tasks

from config import Config, load_config
from database import ReminderDatabase

JST = timezone(timedelta(hours=9), name="JST")
logger = logging.getLogger("rga_assistant")


def is_monitored_channel(
    category_id: int | None,
    channel_id: int,
    category_ids: tuple[int, ...],
    excluded_channel_ids: tuple[int, ...],
) -> bool:
    return (
        category_id in category_ids
        and channel_id not in excluded_channel_ids
    )


def is_reminder_cancel_message(
    content: str,
    channel_category_id: int | None,
    configured_category_ids: tuple[int, ...],
    channel_id: int,
    excluded_channel_ids: tuple[int, ...],
    trigger_text: str,
) -> bool:
    return (
        channel_category_id in configured_category_ids
        and channel_id not in excluded_channel_ids
        and content == trigger_text
    )


def elapsed_days(opened_at: datetime, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    return (now - opened_at.astimezone(timezone.utc)).days


def build_ticket_reminder_message(
    channel_name: str,
    opened_at: datetime,
    days: int,
    guild_id: str,
    channel_id: str,
) -> str:
    opened_at_jst = opened_at.astimezone(JST)
    return (
        "**チケット確認リマインダー**\n\n"
        f"チケット **{discord.utils.escape_markdown(channel_name)}** "
        f"の開設から{days}日が経過しました。\n"
        "もし申請をしているのにも関わらず、この文章が送られた場合は、「審査をお願いします。」という文章が読み取れなかった場合があります。お問い合わせください。"
        "申請者は、もし申請を取り下げる場合、Endというボタンを押してください。\n\n"
        f"開設日時：{opened_at_jst:%Y/%m/%d %H:%M} JST\n"
        f"チケット：https://discord.com/channels/{guild_id}/{channel_id}"
    )


def build_operations_reminder_message(
    channel_name: str,
    opened_at: datetime,
    days: int,
    guild_id: str,
    channel_id: str,
    mention: str | None = None,
) -> str:
    opened_at_jst = opened_at.astimezone(JST)
    prefix = f"{mention}\n" if mention else ""
    return (
        f"{prefix}**チケット確認リマインダー**\n\n"
        f"チケット **{discord.utils.escape_markdown(channel_name)}** "
        f"の開設から{days}日が経過しました。\n\n"
        "対応状況を確認してください。\n\n"
        f"開設日時：{opened_at_jst:%Y/%m/%d %H:%M} JST\n"
        f"チケット：https://discord.com/channels/{guild_id}/{channel_id}"
    )


def build_failure_notification(
    guild_id: str,
    channel_id: str,
    target: str,
    attempts: int,
    error: str,
) -> str:
    return (
        "**チケットリマインダー送信失敗**\n\n"
        f"チャンネル：https://discord.com/channels/{guild_id}/{channel_id}\n"
        f"送信先：{target}\n"
        f"試行回数：{attempts}\n"
        f"理由：{error}\n\n"
        "`python -m tools.retry` で再試行対象に戻せます。"
    )


class RGAAssistant(discord.Client):
    def __init__(self, config: Config, database: ReminderDatabase) -> None:
        super().__init__(
            intents=discord.Intents(guilds=True, messages=True, message_content=True)
        )
        self.config = config
        self.database = database

    async def setup_hook(self) -> None:
        self.database.initialize()
        self.reminder_loop.start()

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
        scanned = 0
        for guild in self.guilds:
            for channel in guild.text_channels:
                if self._is_monitored_ticket_channel(channel):
                    scanned += 1
                    self._register_ticket(channel)
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
            self._register_ticket(channel)
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
            self._register_ticket(after)

    async def on_message(self, message: discord.Message) -> None:
        if (
            message.author.bot
            or message.guild is None
            or not isinstance(message.channel, discord.TextChannel)
        ):
            return

        if self._matches_trigger(message, self.config.reminder_trigger_text):
            if self.database.cancel(message.channel.id):
                logger.info(
                    "Cancelled ticket reminder after trigger text in channel %s",
                    message.channel.id,
                )
            return

        if self.config.reopen_trigger_text and self._matches_trigger(
            message, self.config.reopen_trigger_text
        ):
            if self.database.reopen(message.channel.id, message.created_at):
                logger.info(
                    "Reopened ticket reminder after trigger text in channel %s",
                    message.channel.id,
                )

    def _matches_trigger(self, message: discord.Message, trigger_text: str) -> bool:
        return is_reminder_cancel_message(
            message.content,
            message.channel.category_id,
            self.config.ticket_category_ids,
            message.channel.id,
            self.config.excluded_channel_ids,
            trigger_text,
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

    def _register_ticket(self, channel: discord.TextChannel) -> None:
        inserted = self.database.register(
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
        if self.database.cancel(channel.id):
            logger.info("Cancelled pending reminder for deleted channel %s", channel.id)

    @tasks.loop(seconds=5)
    async def reminder_loop(self) -> None:
        try:
            due = self.database.claim_due()
        except sqlite3.Error:
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

    async def _send_delivery(self, delivery: dict[str, str]) -> None:
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
                self.database.cancel(int(delivery["channel_id"]))
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
        except sqlite3.Error:
            logger.exception(
                "Failed to check destination channel %s", delivery["channel_id"]
            )
            return

        if not isinstance(channel, discord.TextChannel):
            if target == "ticket":
                self.database.cancel(int(delivery["channel_id"]))
                logger.info(
                    "Ticket channel %s is no longer available",
                    delivery["channel_id"],
                )
            else:
                await self._record_failure(
                    delivery, "destination is not a text channel", permanent=True
                )
            return

        if not self.database.is_sending(delivery["channel_id"], target):
            return

        opened_at = datetime.fromisoformat(delivery["opened_at"])
        days = elapsed_days(opened_at)
        if target == "operations":
            mention = None
            role_id = self.config.operations_mention_role_id
            if role_id is not None:
                mention = f"<@&{role_id}>"
            message = build_operations_reminder_message(
                delivery["channel_name"],
                opened_at,
                days,
                delivery["guild_id"],
                delivery["channel_id"],
                mention,
            )
            allowed_mentions = (
                discord.AllowedMentions(roles=[discord.Object(id=role_id)])
                if role_id is not None
                else discord.AllowedMentions.none()
            )
        else:
            message = build_ticket_reminder_message(
                delivery["channel_name"],
                opened_at,
                days,
                delivery["guild_id"],
                delivery["channel_id"],
            )
            allowed_mentions = discord.AllowedMentions.none()

        try:
            sent_message = await channel.send(message, allowed_mentions=allowed_mentions)
        except discord.Forbidden:
            await self._record_failure(
                delivery, "missing permission to send message", permanent=True
            )
            return
        except discord.HTTPException as exc:
            await self._record_failure(delivery, f"HTTPException: {exc}", permanent=False)
            return
        except sqlite3.Error:
            logger.exception(
                "Failed to send %s reminder for ticket %s",
                target,
                delivery["channel_id"],
            )
            return

        self.database.mark_delivery_sent(delivery["channel_id"], target)
        logger.info(
            "Sent %s reminder: channel_id=%s message_id=%s",
            target,
            delivery["channel_id"],
            sent_message.id,
        )

    async def _record_failure(
        self,
        delivery: dict[str, str],
        error: str,
        permanent: bool,
    ) -> None:
        try:
            status, attempts = self.database.fail_delivery(
                delivery["channel_id"], delivery["target"], error, permanent
            )
        except sqlite3.Error:
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
        delivery: dict[str, str],
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
                build_failure_notification(
                    delivery["guild_id"],
                    delivery["channel_id"],
                    delivery["target"],
                    attempts,
                    error,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.HTTPException, sqlite3.Error):
            logger.exception("Failed to notify operations about reminder failure")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    database = ReminderDatabase(
        config.database_path,
        config.reminder_after,
        config.reminder_targets,
        retry_base_seconds=config.retry_base_seconds,
        retry_max_seconds=config.retry_max_seconds,
        retry_max_attempts=config.retry_max_attempts,
    )
    client = RGAAssistant(config, database)
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
