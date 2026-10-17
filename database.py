import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

_CREATE_REMINDERS = """
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

_CREATE_DELIVERIES = """
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    channel_id TEXT NOT NULL,
    target TEXT NOT NULL CHECK (target IN ('ticket', 'operations')),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'sending', 'sent', 'cancelled', 'error')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    sent_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, target)
)
"""

_CREATE_LECTURES = """
CREATE TABLE IF NOT EXISTS lectures (
    lecture_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    title TEXT NOT NULL,
    lecturer TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at TEXT NOT NULL,
    announce_channel_id TEXT NOT NULL,
    reminder_lead_minutes INTEGER NOT NULL,
    reminder_due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'cancelled')),
    reminder_status TEXT NOT NULL CHECK (
        reminder_status IN ('pending', 'sending', 'sent', 'cancelled', 'expired')
    ),
    reminder_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    reminder_sent_at TEXT,
    announcement_message_id TEXT,
    reminder_message_id TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class ReminderDatabase:
    def __init__(
        self,
        path: Path,
        reminder_after: timedelta,
        targets: tuple[str, ...] = ("ticket",),
        retry_base_seconds: float = 60.0,
        retry_max_seconds: float = 21600.0,
        retry_max_attempts: int = 6,
    ) -> None:
        self.path = path
        if reminder_after <= timedelta(0):
            raise ValueError("reminder_after must be positive.")
        if not targets:
            raise ValueError("targets must not be empty.")
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive.")
        if retry_max_attempts <= 0:
            raise ValueError("retry_max_attempts must be positive.")
        self.reminder_after = reminder_after
        self.targets = tuple(targets)
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.retry_max_attempts = retry_max_attempts

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(_CREATE_REMINDERS)
            connection.execute(_CREATE_DELIVERIES)
            connection.execute(_CREATE_LECTURES)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION:
                self._migrate(connection, version)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            # Stale in-flight deliveries are returned to pending after a restart.
            connection.execute(
                """
                UPDATE reminder_deliveries
                SET status = 'pending', updated_at = ?
                WHERE status = 'sending'
                """,
                (self._now().isoformat(),),
            )

    def _migrate(self, connection: sqlite3.Connection, version: int) -> None:
        if version < 1:
            # Backfill a single ticket delivery for reminders created before the
            # per-destination delivery table existed.
            connection.execute(
                """
                INSERT OR IGNORE INTO reminder_deliveries
                    (channel_id, target, status, attempts, next_attempt_at,
                     sent_at, updated_at)
                SELECT channel_id, 'ticket',
                    CASE status
                        WHEN 'sent' THEN 'sent'
                        WHEN 'cancelled' THEN 'cancelled'
                        ELSE 'pending'
                    END,
                    0, NULL, sent_at, updated_at
                FROM ticket_reminders
                """
            )
            connection.execute(
                "UPDATE ticket_reminders SET status = 'pending' WHERE status = 'sending'"
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
            inserted = cursor.rowcount > 0
            if inserted:
                self._insert_deliveries(connection, str(channel_id), now)
            return inserted

    def cancel(self, channel_id: int) -> bool:
        now = self._now().isoformat()
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'cancelled', updated_at = ?
                WHERE channel_id = ? AND status = 'pending'
                """,
                (now, str(channel_id)),
            )
            connection.execute(
                """
                UPDATE reminder_deliveries
                SET status = 'cancelled', updated_at = ?
                WHERE channel_id = ? AND status IN ('pending', 'sending')
                """,
                (now, str(channel_id)),
            )
            return cursor.rowcount > 0

    def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = (now or self._now()).astimezone(timezone.utc)
        iso = now.isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT d.channel_id AS channel_id,
                       d.target AS target,
                       d.attempts AS attempts,
                       r.guild_id AS guild_id,
                       r.channel_name AS channel_name,
                       r.opened_at AS opened_at,
                       r.due_at AS due_at
                FROM reminder_deliveries d
                JOIN ticket_reminders r ON r.channel_id = d.channel_id
                WHERE r.status = 'pending'
                  AND d.status = 'pending'
                  AND r.due_at <= ?
                  AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= ?)
                ORDER BY r.due_at
                """,
                (iso, iso),
            ).fetchall()
            if rows:
                connection.executemany(
                    """
                    UPDATE reminder_deliveries
                    SET status = 'sending', updated_at = ?
                    WHERE channel_id = ? AND target = ? AND status = 'pending'
                    """,
                    [(iso, row["channel_id"], row["target"]) for row in rows],
                )
            return [dict(row) for row in rows]

    def is_sending(self, channel_id: str, target: str) -> bool:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM reminder_deliveries
                WHERE channel_id = ? AND target = ? AND status = 'sending'
                """,
                (str(channel_id), target),
            ).fetchone()
            return row is not None

    def mark_delivery_sent(self, channel_id: str, target: str) -> None:
        now = self._now().isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE reminder_deliveries
                SET status = 'sent', sent_at = ?, updated_at = ?
                WHERE channel_id = ? AND target = ? AND status = 'sending'
                """,
                (now, now, str(channel_id), target),
            )
            connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'sent', sent_at = ?, updated_at = ?
                WHERE channel_id = ? AND status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM reminder_deliveries
                      WHERE channel_id = ? AND status = 'sent'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM reminder_deliveries
                      WHERE channel_id = ?
                        AND status IN ('pending', 'sending', 'error')
                  )
                """,
                (now, now, str(channel_id), str(channel_id), str(channel_id)),
            )

    def fail_delivery(
        self,
        channel_id: str,
        target: str,
        error: str,
        permanent: bool = False,
        now: datetime | None = None,
    ) -> tuple[str, int]:
        now = (now or self._now()).astimezone(timezone.utc)
        message = (error or "")[:500]
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT attempts FROM reminder_deliveries
                WHERE channel_id = ? AND target = ? AND status = 'sending'
                """,
                (str(channel_id), target),
            ).fetchone()
            if row is None:
                return ("unknown", 0)
            attempts = row[0] + 1
            if permanent or attempts >= self.retry_max_attempts:
                status = "error"
                next_attempt_at = None
            else:
                status = "pending"
                delay = min(
                    self.retry_max_seconds,
                    self.retry_base_seconds * (2 ** (attempts - 1)),
                )
                next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
            connection.execute(
                """
                UPDATE reminder_deliveries
                SET status = ?, attempts = ?, next_attempt_at = ?,
                    last_error = ?, updated_at = ?
                WHERE channel_id = ? AND target = ?
                """,
                (
                    status,
                    attempts,
                    next_attempt_at,
                    message,
                    now.isoformat(),
                    str(channel_id),
                    target,
                ),
            )
            return (status, attempts)

    def retry_errors(self, channel_id: int | None = None) -> int:
        now = self._now().isoformat()
        with sqlite3.connect(self.path) as connection:
            if channel_id is None:
                cursor = connection.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = 'pending', next_attempt_at = NULL, updated_at = ?
                    WHERE status = 'error'
                    """,
                    (now,),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = 'pending', next_attempt_at = NULL, updated_at = ?
                    WHERE status = 'error' AND channel_id = ?
                    """,
                    (now, str(channel_id)),
                )
            return cursor.rowcount

    def list_errors(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT channel_id, target, attempts, last_error, updated_at
                FROM reminder_deliveries
                WHERE status = 'error'
                ORDER BY updated_at
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def get_delivery(self, channel_id: str, target: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM reminder_deliveries
                WHERE channel_id = ? AND target = ?
                """,
                (str(channel_id), target),
            ).fetchone()
            return dict(row) if row is not None else None

    def get_reminder(self, channel_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM ticket_reminders WHERE channel_id = ?",
                (str(channel_id),),
            ).fetchone()
            return dict(row) if row is not None else None

    def _insert_deliveries(
        self, connection: sqlite3.Connection, channel_id: str, now: str
    ) -> None:
        connection.executemany(
            """
            INSERT OR IGNORE INTO reminder_deliveries
                (channel_id, target, status, attempts, next_attempt_at, updated_at)
            VALUES (?, ?, 'pending', 0, NULL, ?)
            """,
            [(channel_id, target, now) for target in self.targets],
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


class LectureDatabase:
    """SQLite access layer for the lecture schedule feature (Phase 3).

    Shares the same database file as :class:`ReminderDatabase` but owns only the
    ``lectures`` table so the ticket reminder logic stays independent.
    """

    ID_LENGTH = 10
    ID_ATTEMPTS = 5

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(_CREATE_LECTURES)
            # Stale in-flight reminders are returned to pending after a restart so
            # a reminder interrupted by a crash is not lost.
            connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'pending', updated_at = ?
                WHERE status = 'scheduled' AND reminder_status = 'sending'
                """,
                (self._now().isoformat(),),
            )

    def create(
        self,
        guild_id: int,
        title: str,
        lecturer: str,
        description: str,
        starts_at: datetime,
        announce_channel_id: int,
        reminder_lead_minutes: int,
        created_by: int | None = None,
    ) -> dict[str, Any]:
        starts_at = starts_at.astimezone(timezone.utc)
        now = self._now()
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            for _ in range(self.ID_ATTEMPTS):
                lecture_id = uuid.uuid4().hex[: self.ID_LENGTH]
                try:
                    connection.execute(
                        """
                        INSERT INTO lectures
                            (lecture_id, guild_id, title, lecturer, description,
                             starts_at, announce_channel_id, reminder_lead_minutes,
                             reminder_due_at, status, reminder_status, created_by,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', 'pending',
                                ?, ?, ?)
                        """,
                        (
                            lecture_id,
                            str(guild_id),
                            title,
                            lecturer,
                            description,
                            starts_at.isoformat(),
                            str(announce_channel_id),
                            reminder_lead_minutes,
                            (starts_at - timedelta(minutes=reminder_lead_minutes)).isoformat(),
                            None if created_by is None else str(created_by),
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                return dict(connection.execute(
                    "SELECT * FROM lectures WHERE lecture_id = ?",
                    (lecture_id,),
                ).fetchone())
        raise RuntimeError("failed to allocate a unique lecture id")

    def get(self, lecture_id: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM lectures WHERE lecture_id = ?",
                (lecture_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_lectures(
        self,
        guild_id: int,
        include_cancelled: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM lectures WHERE guild_id = ?"
        parameters: list[Any] = [str(guild_id)]
        if not include_cancelled:
            query += " AND status != 'cancelled'"
        query += " ORDER BY starts_at"
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
            return [dict(row) for row in rows]

    def update(
        self,
        lecture_id: str,
        title: str | None = None,
        lecturer: str | None = None,
        description: str | None = None,
        starts_at: datetime | None = None,
        announce_channel_id: int | None = None,
        reminder_lead_minutes: int | None = None,
    ) -> dict[str, Any] | None:
        current = self.get(lecture_id)
        if current is None:
            return None

        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title
        if lecturer is not None:
            fields["lecturer"] = lecturer
        if description is not None:
            fields["description"] = description
        if announce_channel_id is not None:
            fields["announce_channel_id"] = str(announce_channel_id)

        lead = (
            reminder_lead_minutes
            if reminder_lead_minutes is not None
            else current["reminder_lead_minutes"]
        )
        if reminder_lead_minutes is not None:
            fields["reminder_lead_minutes"] = reminder_lead_minutes
        if starts_at is not None or reminder_lead_minutes is not None:
            effective_start = starts_at or datetime.fromisoformat(current["starts_at"])
            fields["reminder_due_at"] = (
                effective_start.astimezone(timezone.utc)
                - timedelta(minutes=lead)
            ).isoformat()
        if starts_at is not None:
            fields["starts_at"] = starts_at.astimezone(timezone.utc).isoformat()

        if not fields:
            return current

        fields["updated_at"] = self._now().isoformat()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                f"UPDATE lectures SET {assignments} WHERE lecture_id = ?",
                (*fields.values(), lecture_id),
            )
        return self.get(lecture_id)

    def cancel(self, lecture_id: str) -> dict[str, Any] | None:
        now = self._now().isoformat()
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE lectures
                SET status = 'cancelled',
                    reminder_status = CASE
                        WHEN reminder_status IN ('pending', 'sending') THEN 'cancelled'
                        ELSE reminder_status
                    END,
                    updated_at = ?
                WHERE lecture_id = ?
                """,
                (now, lecture_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get(lecture_id)

    def delete(self, lecture_id: str) -> bool:
        with sqlite3.connect(self.path) as connection:
            cursor = connection.execute(
                "DELETE FROM lectures WHERE lecture_id = ?",
                (lecture_id,),
            )
            return cursor.rowcount > 0

    def set_announcement_message(self, lecture_id: str, message_id: int) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE lectures
                SET announcement_message_id = ?, updated_at = ?
                WHERE lecture_id = ?
                """,
                (str(message_id), self._now().isoformat(), lecture_id),
            )

    def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Claim lecture reminders that are due, expiring ones that already started."""
        now = (now or self._now()).astimezone(timezone.utc)
        iso = now.isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            # A reminder only makes sense before the lecture starts; anything that
            # already began (e.g. while the bot was restarting) is expired instead.
            connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'expired', updated_at = ?
                WHERE status = 'scheduled'
                  AND reminder_status IN ('pending', 'sending')
                  AND starts_at <= ?
                """,
                (iso, iso),
            )
            rows = connection.execute(
                """
                SELECT * FROM lectures
                WHERE status = 'scheduled'
                  AND reminder_status = 'pending'
                  AND reminder_due_at <= ?
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY reminder_due_at
                """,
                (iso, iso),
            ).fetchall()
            if rows:
                connection.executemany(
                    """
                    UPDATE lectures
                    SET reminder_status = 'sending', updated_at = ?
                    WHERE lecture_id = ? AND reminder_status = 'pending'
                    """,
                    [(iso, row["lecture_id"]) for row in rows],
                )
            return [dict(row) for row in rows]

    def mark_reminder_sent(self, lecture_id: str, message_id: int) -> None:
        now = self._now().isoformat()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'sent', reminder_message_id = ?,
                    reminder_sent_at = ?, last_error = NULL, updated_at = ?
                WHERE lecture_id = ? AND reminder_status = 'sending'
                """,
                (str(message_id), now, now, lecture_id),
            )

    def release_reminder(
        self,
        lecture_id: str,
        error: str,
        retry_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> None:
        """Return a failed reminder to pending so it is retried before the start."""
        now = (now or self._now()).astimezone(timezone.utc)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'pending',
                    reminder_attempts = reminder_attempts + 1,
                    next_attempt_at = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE lecture_id = ? AND reminder_status = 'sending'
                """,
                (
                    (now + timedelta(seconds=retry_seconds)).isoformat(),
                    (error or "")[:500],
                    now.isoformat(),
                    lecture_id,
                ),
            )

    def expire_reminder(self, lecture_id: str, error: str) -> None:
        """Give up on a reminder that cannot be delivered (permanent failure)."""
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'expired',
                    reminder_attempts = reminder_attempts + 1,
                    next_attempt_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE lecture_id = ? AND reminder_status = 'sending'
                """,
                ((error or "")[:500], self._now().isoformat(), lecture_id),
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
