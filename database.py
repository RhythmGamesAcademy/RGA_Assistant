import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ReminderDatabase:
    def __init__(self, path: Path, reminder_after: timedelta) -> None:
        self.path = path
        if reminder_after <= timedelta(0):
            raise ValueError("reminder_after must be positive.")
        self.reminder_after = reminder_after

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_reminders (
                    channel_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'sending', 'sent', 'cancelled')
                    ),
                    sent_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'pending', updated_at = ?
                WHERE status = 'sending'
                """,
                (self._now().isoformat(),),
            )

    def register(
        self,
        channel_id: int,
        guild_id: int,
        channel_name: str,
        opened_at: datetime,
    ) -> bool:
        opened_at = opened_at.astimezone(timezone.utc)
        now = self._now().isoformat()
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO ticket_reminders
                    (channel_id, guild_id, channel_name, opened_at, due_at,
                     status, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    str(channel_id),
                    str(guild_id),
                    channel_name,
                    opened_at.isoformat(),
                    (opened_at + self.reminder_after).isoformat(),
                    now,
                ),
            )
            return cursor.rowcount > 0

    def cancel(self, channel_id: int) -> bool:
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'cancelled', updated_at = ?
                WHERE channel_id = ? AND status IN ('pending', 'sending')
                """,
                (self._now().isoformat(), str(channel_id)),
            )
            return cursor.rowcount > 0

    def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = (now or self._now()).astimezone(timezone.utc)
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM ticket_reminders
                WHERE status = 'pending' AND due_at <= ?
                ORDER BY due_at
                """,
                (now.isoformat(),),
            ).fetchall()
            if rows:
                connection.executemany(
                    """
                    UPDATE ticket_reminders SET status = 'sending', updated_at = ?
                    WHERE channel_id = ? AND status = 'pending'
                    """,
                    [(now.isoformat(), row["channel_id"]) for row in rows],
                )
            return [dict(row) for row in rows]

    def mark_sent(self, channel_id: str) -> None:
        now = self._now().isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'sent', sent_at = ?, updated_at = ?
                WHERE channel_id = ? AND status = 'sending'
                """,
                (now, now, channel_id),
            )

    def is_sending(self, channel_id: str) -> bool:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM ticket_reminders
                WHERE channel_id = ? AND status = 'sending'
                """,
                (channel_id,),
            ).fetchone()
            return row is not None

    def retry(self, channel_id: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'pending', updated_at = ?
                WHERE channel_id = ? AND status = 'sending'
                """,
                (self._now().isoformat(), channel_id),
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
