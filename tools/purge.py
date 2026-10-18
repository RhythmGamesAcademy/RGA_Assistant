"""Delete finished reminder and lecture rows older than the retention window.

Usage (run from the repository root, after setting ``RGA_DATABASE_URL``):

    python -m tools.purge             # delete rows older than RGA_RETENTION_DAYS
    python -m tools.purge --days 30   # override the retention window
    python -m tools.purge --dry-run   # only report what would be deleted

Only finished rows are removed: ticket reminders in ``sent``/``cancelled`` and
lectures that are cancelled or whose reminder reached a terminal state. Pending
and errored reminders are kept so the retry tooling can still process them.
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone

from config import Config, load_config
from database import (
    LectureDatabase,
    ReminderDatabase,
    ensure_compatible_event_loop_policy,
    open_pool,
)


def build_reminder_database(config: Config, pool) -> ReminderDatabase:
    return ReminderDatabase(
        pool,
        config.reminder_after,
        config.reminder_targets,
        retry_base_seconds=config.retry_base_seconds,
        retry_max_seconds=config.retry_max_seconds,
        retry_max_attempts=config.retry_max_attempts,
    )


async def run(args: argparse.Namespace, config: Config) -> int:
    days = args.days if args.days is not None else config.retention_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    pool = await open_pool(config.database_url)
    try:
        # Create the schema when missing without touching in-flight state, so this
        # CLI is safe to run from cron while the bot is up.
        reminders = build_reminder_database(config, pool)
        lectures = LectureDatabase(pool)
        await reminders.ensure_schema()
        await lectures.ensure_schema()

        if args.dry_run:
            reminder_count = await reminders.count_purgeable(cutoff)
            lecture_count = await lectures.count_purgeable(cutoff)
            print(
                f"{days}日より古い終了分: "
                f"チケットリマインダー {reminder_count}件 / 講義 {lecture_count}件"
                "（--dry-run のため削除していません）。"
            )
            return 0

        reminder_count = await reminders.purge_old(cutoff)
        lecture_count = await lectures.purge_old(cutoff)
        print(
            f"{days}日より古い終了分を削除しました: "
            f"チケットリマインダー {reminder_count}件 / 講義 {lecture_count}件。"
        )
        return 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="保存期間を過ぎた終了済みデータを削除します。"
        " 対象は sent/cancelled のチケットリマインダーと、中止または送信済みの講義です。"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="保持日数。省略すると RGA_RETENTION_DAYS（既定90）を使用します。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="削除せず、対象件数だけを表示します。",
    )
    args = parser.parse_args(argv)
    if args.days is not None and args.days <= 0:
        parser.error("--days must be a positive integer.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    ensure_compatible_event_loop_policy()

    try:
        config = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    return asyncio.run(run(args, config))


if __name__ == "__main__":
    sys.exit(main())
