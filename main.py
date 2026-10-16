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
            or not is_reminder_cancel_message(
                message.content,
                message.channel.category_id,
                self.config.ticket_category_ids,
                message.channel.id,
                self.config.excluded_channel_ids,
                self.config.reminder_trigger_text,
            )
        ):
            return

        if self.database.cancel(message.channel.id):
            logger.info(
                "Cancelled ticket reminder after trigger text in channel %s",
                message.channel.id,
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
                "Scheduled reminder: channel_id=%s created_at=%s due_at=%s",
                channel.id,
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
        for reminder in self.database.claim_due():
            await self._send_reminder(reminder)

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.wait_until_ready()

    async def _send_reminder(self, reminder: dict[str, str]) -> None:
        try:
            ticket = await self.fetch_channel(int(reminder["channel_id"]))
        except discord.NotFound:
            self.database.cancel(int(reminder["channel_id"]))
            logger.info("Cancelled reminder for unavailable ticket %s", reminder["channel_id"])
            return
        except (discord.HTTPException, sqlite3.Error):
            self.database.retry(reminder["channel_id"])
            logger.exception("Failed to check ticket channel %s", reminder["channel_id"])
            return
        try:
            if not isinstance(ticket, discord.TextChannel):
                self.database.cancel(int(reminder["channel_id"]))
                logger.info("Ticket channel %s is no longer available", reminder["channel_id"])
                return
            opened_at = datetime.fromisoformat(reminder["opened_at"]).astimezone(JST)
            elapsed_days = (datetime.now(timezone.utc) - opened_at.astimezone(timezone.utc)).days
            message = (
                "**チケット確認リマインダー**\n\n"
                f"チケット **{discord.utils.escape_markdown(reminder['channel_name'])}** "
                f"の開設から{elapsed_days}日が経過しました。\n"
                "もし申請をしているのにも関わらず、この文章が送られた場合は、「審査をお願いします。」という文章が読み取れなかった場合があります。お問い合わせください。"
                "申請者は、もし申請を取り下げる場合、Endというボタンを押してください。\n\n"
                f"開設日時：{opened_at:%Y/%m/%d %H:%M} JST\n"
                f"チケット：https://discord.com/channels/"
                f"{reminder['guild_id']}/{reminder['channel_id']}"
            )
            if not self.database.is_sending(reminder["channel_id"]):
                return
            sent_message = await ticket.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.database.mark_sent(reminder["channel_id"])
            logger.info(
                "Sent reminder: channel_id=%s message_id=%s",
                reminder["channel_id"],
                sent_message.id,
            )
        except (discord.HTTPException, TypeError, sqlite3.Error):
            self.database.retry(reminder["channel_id"])
            logger.exception("Failed to send reminder for ticket %s", reminder["channel_id"])


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    database = ReminderDatabase(config.database_path, config.reminder_after)
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
