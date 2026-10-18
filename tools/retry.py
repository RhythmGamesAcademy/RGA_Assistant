"""Reset failed reminder deliveries back to pending.

Usage (run from the repository root):

    python -m tools.retry            # reset every failed delivery
    python -m tools.retry 1234567890 # reset only this ticket channel
    python -m tools.retry --list     # show failed deliveries
"""

import argparse
import asyncio
import logging
import sys

from config import Config, load_config
from database import ReminderDatabase, ensure_compatible_event_loop_policy, open_pool


def build_database(config: Config, pool) -> ReminderDatabase:
    return ReminderDatabase(
        pool,
        config.reminder_after,
        config.reminder_targets,
        retry_base_seconds=config.retry_base_seconds,
        retry_max_seconds=config.retry_max_seconds,
        retry_max_attempts=config.retry_max_attempts,
    )


async def run(args: argparse.Namespace, config: Config) -> int:
    pool = await open_pool(config.database_url)
    try:
        database = build_database(config, pool)
        await database.initialize()

        if args.list:
            errors = await database.list_errors()
            if not errors:
                print("エラー状態の送信はありません。")
                return 0
            for row in errors:
                print(
                    f"channel_id={row['channel_id']} target={row['target']} "
                    f"attempts={row['attempts']} updated_at={row['updated_at']} "
                    f"error={row['last_error']}"
                )
            return 0

        count = await database.retry_errors(args.channel_id)
        print(f"{count}件の送信を再試行対象に戻しました。")
        return 0
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="チケットリマインダーの送信失敗を再試行対象に戻します。"
    )
    parser.add_argument(
        "channel_id",
        nargs="?",
        type=int,
        help="対象のチケットチャンネルID。省略すると全ての失敗を対象にします。",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="失敗状態の送信を一覧表示して終了します。",
    )
    args = parser.parse_args(argv)

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
