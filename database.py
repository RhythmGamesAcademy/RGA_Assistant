import asyncio
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger("rga_assistant")

SCHEMA_VERSION = 4

_CREATE_TICKET_REMINDERS = """
CREATE TABLE IF NOT EXISTS ticket_reminders (
    channel_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_name TEXT NOT NULL,
    opened_at TIMESTAMPTZ NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'sending', 'sent', 'cancelled')
    ),
    sent_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

_CREATE_DELIVERIES = """
CREATE TABLE IF NOT EXISTS reminder_deliveries (
    channel_id TEXT NOT NULL
        REFERENCES ticket_reminders (channel_id) ON DELETE CASCADE,
    target TEXT NOT NULL CHECK (target IN ('ticket', 'operations')),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'sending', 'sent', 'cancelled', 'error')
    ),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    sent_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (channel_id, target)
)
"""

_CREATE_APPLICATION_IMAGE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS application_image_notifications (
    channel_id TEXT PRIMARY KEY
        REFERENCES ticket_reminders (channel_id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent')),
    notified_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

_CREATE_APPLICATION_STATUS = """
CREATE TABLE IF NOT EXISTS ticket_application_status (
    channel_id TEXT PRIMARY KEY
        REFERENCES ticket_reminders (channel_id) ON DELETE CASCADE,
    has_application BOOLEAN,
    detail TEXT,
    confidence REAL,
    method TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

_CREATE_LECTURES = """
CREATE TABLE IF NOT EXISTS lectures (
    lecture_id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    title TEXT NOT NULL,
    lecturer TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    starts_at TIMESTAMPTZ NOT NULL,
    announce_channel_id TEXT NOT NULL,
    reminder_lead_minutes INTEGER NOT NULL,
    reminder_due_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'cancelled')),
    reminder_status TEXT NOT NULL CHECK (
        reminder_status IN ('pending', 'sending', 'sent', 'cancelled', 'expired')
    ),
    reminder_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    reminder_sent_at TIMESTAMPTZ,
    announcement_message_id TEXT,
    reminder_message_id TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

_CREATE_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    version INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

_CREATE_TICKET_TODOS = """
CREATE TABLE IF NOT EXISTS ticket_todos (
    channel_id TEXT PRIMARY KEY
        REFERENCES ticket_reminders (channel_id) ON DELETE CASCADE,
    todo_id TEXT,
    decided BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'registered', 'error')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL
)
"""

_CREATE_TICKET_TODO_DECISIONS = """
CREATE TABLE IF NOT EXISTS ticket_todo_decisions (
    channel_id TEXT NOT NULL
        REFERENCES ticket_todos (channel_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approval', 'reject')),
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (channel_id, user_id)
)
"""

_UPSERT_SCHEMA_VERSION = """
INSERT INTO schema_version (id, version, updated_at)
VALUES (TRUE, %s, %s)
ON CONFLICT (id) DO UPDATE
SET version = EXCLUDED.version, updated_at = EXCLUDED.updated_at
WHERE schema_version.version < EXCLUDED.version
"""


def ensure_compatible_event_loop_policy() -> None:
    """Select an event loop policy that psycopg's async connections support.

    psycopg refuses to run on Windows' default ``ProactorEventLoop``, so this must
    be called *before* the loop is created (``asyncio.run`` / ``Client.run``).
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def open_pool(
    url: str,
    min_size: int = 1,
    max_size: int = 5,
) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        url,
        kwargs={"row_factory": dict_row},
        min_size=min_size,
        max_size=max_size,
        open=False,
    )
    await pool.open()
    await pool.wait()
    return pool


async def _create_schema(connection: Any, now: datetime) -> None:
    await connection.execute(_CREATE_TICKET_REMINDERS)
    await connection.execute(_CREATE_DELIVERIES)
    await connection.execute(_CREATE_APPLICATION_IMAGE_NOTIFICATIONS)
    await connection.execute(_CREATE_APPLICATION_STATUS)
    await connection.execute(_CREATE_LECTURES)
    await connection.execute(_CREATE_TICKET_TODOS)
    await connection.execute(_CREATE_TICKET_TODO_DECISIONS)
    await connection.execute(_CREATE_SCHEMA_VERSION)
    await connection.execute(_UPSERT_SCHEMA_VERSION, (SCHEMA_VERSION, now))


class ReminderDatabase:
    """PostgreSQL access layer for ticket reminders.

    The connection pool is created once by the caller and shared with
    :class:`LectureDatabase`.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        reminder_after: timedelta,
        targets: tuple[str, ...] = ("ticket",),
        retry_base_seconds: float = 60.0,
        retry_max_seconds: float = 21600.0,
        retry_max_attempts: int = 6,
    ) -> None:
        if reminder_after <= timedelta(0):
            raise ValueError("reminder_after must be positive.")
        if not targets:
            raise ValueError("targets must not be empty.")
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive.")
        if retry_max_attempts <= 0:
            raise ValueError("retry_max_attempts must be positive.")
        self.pool = pool
        self.reminder_after = reminder_after
        self.targets = tuple(targets)
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.retry_max_attempts = retry_max_attempts

    async def ensure_schema(self) -> None:
        """Create the schema without touching any stored state."""
        async with self.pool.connection() as connection:
            await _create_schema(connection, self._now())

    async def initialize(self) -> None:
        async with self.pool.connection() as connection:
            await _create_schema(connection, self._now())
            # Stale in-flight deliveries are returned to pending after a restart.
            await connection.execute(
                """
                UPDATE reminder_deliveries
                SET status = 'pending', updated_at = %s
                WHERE status = 'sending'
                """,
                (self._now(),),
            )
            await connection.execute(
                """
                UPDATE application_image_notifications
                SET status = 'pending', updated_at = %s
                WHERE status = 'sending'
                """,
                (self._now(),),
            )

    async def register(
        self,
        channel_id: int,
        guild_id: int,
        channel_name: str,
        opened_at: datetime,
    ) -> bool:
        opened_at = opened_at.astimezone(timezone.utc)
        now = self._now()
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO ticket_reminders
                    (channel_id, guild_id, channel_name, opened_at, due_at,
                     status, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'pending', %s)
                ON CONFLICT (channel_id) DO NOTHING
                RETURNING channel_id
                """,
                (
                    str(channel_id),
                    str(guild_id),
                    channel_name,
                    opened_at,
                    opened_at + self.reminder_after,
                    now,
                ),
            )
            inserted = await cursor.fetchone() is not None
            if inserted:
                await self._insert_deliveries(connection, str(channel_id), now)
            return inserted

    async def cancel(self, channel_id: int) -> bool:
        now = self._now()
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'cancelled', updated_at = %s
                WHERE channel_id = %s AND status = 'pending'
                """,
                (now, str(channel_id)),
            )
            await connection.execute(
                """
                UPDATE reminder_deliveries
                SET status = 'cancelled', updated_at = %s
                WHERE channel_id = %s AND status IN ('pending', 'sending')
                """,
                (now, str(channel_id)),
            )
            return cursor.rowcount > 0

    async def get_application_status(self, channel_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT channel_id, has_application, detail, confidence, method,
                       updated_at
                FROM ticket_application_status
                WHERE channel_id = %s
                """,
                (str(channel_id),),
            )
            return await cursor.fetchone()

    async def set_application_status(
        self,
        channel_id: str,
        has_application: bool | None,
        method: str,
        detail: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Store the latest application status for one ticket channel.

        Rows are persisted independently of the reminder lifecycle so the
        status survives restarts and keeps updating after a reminder is sent
        or cancelled.
        """
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticket_application_status
                    (channel_id, has_application, detail, confidence, method,
                     updated_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (channel_id) DO UPDATE
                SET has_application = EXCLUDED.has_application,
                    detail = EXCLUDED.detail,
                    confidence = EXCLUDED.confidence,
                    method = EXCLUDED.method,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(channel_id),
                    has_application,
                    detail,
                    confidence,
                    method,
                    now,
                ),
            )

    async def claim_application_image_notification(self, channel_id: int) -> bool:
        """Claim the one allowed application-image notification for a ticket."""
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO application_image_notifications
                    (channel_id, status, updated_at)
                VALUES (%s, 'pending', %s)
                ON CONFLICT (channel_id) DO NOTHING
                """,
                (str(channel_id), now),
            )
            cursor = await connection.execute(
                """
                UPDATE application_image_notifications
                SET status = 'sending', updated_at = %s
                WHERE channel_id = %s AND status = 'pending'
                RETURNING channel_id
                """,
                (now, str(channel_id)),
            )
            return await cursor.fetchone() is not None

    async def mark_application_image_notification_sent(self, channel_id: int) -> None:
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE application_image_notifications
                SET status = 'sent', notified_at = %s, updated_at = %s
                WHERE channel_id = %s AND status = 'sending'
                """,
                (now, now, str(channel_id)),
            )

    async def release_application_image_notification(self, channel_id: int) -> None:
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE application_image_notifications
                SET status = 'pending', updated_at = %s
                WHERE channel_id = %s AND status = 'sending'
                """,
                (now, str(channel_id)),
            )

    async def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = (now or self._now()).astimezone(timezone.utc)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT d.channel_id AS channel_id,
                           d.target AS target,
                           d.attempts AS attempts,
                           r.guild_id AS guild_id,
                           r.channel_name AS channel_name,
                           r.opened_at AS opened_at,
                           r.due_at AS due_at
                    FROM reminder_deliveries d
                    JOIN ticket_reminders r USING (channel_id)
                    WHERE r.status = 'pending'
                      AND d.status = 'pending'
                      AND r.due_at <= %s
                      AND (d.next_attempt_at IS NULL OR d.next_attempt_at <= %s)
                    ORDER BY r.due_at
                    FOR UPDATE OF d SKIP LOCKED
                    """,
                    (now, now),
                )
                rows = await cursor.fetchall()
                if rows:
                    await cursor.executemany(
                        """
                        UPDATE reminder_deliveries
                        SET status = 'sending', updated_at = %s
                        WHERE channel_id = %s AND target = %s
                          AND status = 'pending'
                        """,
                        [(now, row["channel_id"], row["target"]) for row in rows],
                    )
                return rows

    async def is_sending(self, channel_id: str, target: str) -> bool:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT 1 FROM reminder_deliveries
                WHERE channel_id = %s AND target = %s AND status = 'sending'
                """,
                (str(channel_id), target),
            )
            return await cursor.fetchone() is not None

    async def mark_delivery_sent(self, channel_id: str, target: str) -> None:
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE reminder_deliveries
                SET status = 'sent', sent_at = %s, updated_at = %s
                WHERE channel_id = %s AND target = %s AND status = 'sending'
                """,
                (now, now, str(channel_id), target),
            )
            await connection.execute(
                """
                UPDATE ticket_reminders
                SET status = 'sent', sent_at = %s, updated_at = %s
                WHERE channel_id = %s AND status = 'pending'
                  AND EXISTS (
                      SELECT 1 FROM reminder_deliveries
                      WHERE channel_id = %s AND status = 'sent'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM reminder_deliveries
                      WHERE channel_id = %s
                        AND status IN ('pending', 'sending', 'error')
                  )
                """,
                (now, now, str(channel_id), str(channel_id), str(channel_id)),
            )

    async def fail_delivery(
        self,
        channel_id: str,
        target: str,
        error: str,
        permanent: bool = False,
        now: datetime | None = None,
    ) -> tuple[str, int]:
        now = (now or self._now()).astimezone(timezone.utc)
        message = (error or "")[:500]
        async with self.pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT attempts FROM reminder_deliveries
                    WHERE channel_id = %s AND target = %s AND status = 'sending'
                    FOR UPDATE
                    """,
                    (str(channel_id), target),
                )
                row = await cursor.fetchone()
                if row is None:
                    return ("unknown", 0)
                attempts = row["attempts"] + 1
                if permanent or attempts >= self.retry_max_attempts:
                    status = "error"
                    next_attempt_at = None
                else:
                    status = "pending"
                    delay = min(
                        self.retry_max_seconds,
                        self.retry_base_seconds * (2 ** (attempts - 1)),
                    )
                    next_attempt_at = now + timedelta(seconds=delay)
                await connection.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = %s, attempts = %s, next_attempt_at = %s,
                        last_error = %s, updated_at = %s
                    WHERE channel_id = %s AND target = %s
                    """,
                    (
                        status,
                        attempts,
                        next_attempt_at,
                        message,
                        now,
                        str(channel_id),
                        target,
                    ),
                )
                return (status, attempts)

    async def retry_errors(self, channel_id: int | None = None) -> int:
        now = self._now()
        async with self.pool.connection() as connection:
            if channel_id is None:
                cursor = await connection.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = 'pending', next_attempt_at = NULL, updated_at = %s
                    WHERE status = 'error'
                    """,
                    (now,),
                )
            else:
                cursor = await connection.execute(
                    """
                    UPDATE reminder_deliveries
                    SET status = 'pending', next_attempt_at = NULL, updated_at = %s
                    WHERE status = 'error' AND channel_id = %s
                    """,
                    (now, str(channel_id)),
                )
            return cursor.rowcount

    async def list_errors(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT channel_id, target, attempts, last_error, updated_at
                FROM reminder_deliveries
                WHERE status = 'error'
                ORDER BY updated_at
                """
            )
            return await cursor.fetchall()

    async def get_delivery(self, channel_id: str, target: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT * FROM reminder_deliveries
                WHERE channel_id = %s AND target = %s
                """,
                (str(channel_id), target),
            )
            return await cursor.fetchone()

    async def get_reminder(self, channel_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM ticket_reminders WHERE channel_id = %s",
                (str(channel_id),),
            )
            return await cursor.fetchone()

    async def count_purgeable(self, before: datetime) -> int:
        """Count terminal reminders last updated before ``before``."""
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT count(*) AS count FROM ticket_reminders
                WHERE status IN ('sent', 'cancelled') AND updated_at < %s
                """,
                (before.astimezone(timezone.utc),),
            )
            row = await cursor.fetchone()
            return int(row["count"])

    async def purge_old(self, before: datetime) -> int:
        """Delete terminal reminders last updated before ``before``.

        Deliveries are removed through the ``ON DELETE CASCADE`` foreign key, so
        only finished tickets (``sent``/``cancelled``) are affected and pending or
        errored reminders are left for the retry tooling.
        """
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                DELETE FROM ticket_reminders
                WHERE status IN ('sent', 'cancelled') AND updated_at < %s
                """,
                (before.astimezone(timezone.utc),),
            )
            return cursor.rowcount

    async def _insert_deliveries(
        self, connection: Any, channel_id: str, now: datetime
    ) -> None:
        for target in self.targets:
            await connection.execute(
                """
                INSERT INTO reminder_deliveries
                    (channel_id, target, status, attempts, next_attempt_at,
                     updated_at)
                VALUES (%s, %s, 'pending', 0, NULL, %s)
                ON CONFLICT (channel_id, target) DO NOTHING
                """,
                (channel_id, target, now),
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


class LectureDatabase:
    """PostgreSQL access layer for the lecture schedule feature (Phase 3)."""

    ID_LENGTH = 10
    ID_ATTEMPTS = 5

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def ensure_schema(self) -> None:
        """Create the schema without touching any stored state."""
        async with self.pool.connection() as connection:
            await _create_schema(connection, self._now())

    async def initialize(self) -> None:
        async with self.pool.connection() as connection:
            await _create_schema(connection, self._now())
            # Stale in-flight reminders are returned to pending after a restart so
            # a reminder interrupted by a crash is not lost.
            await connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'pending', updated_at = %s
                WHERE status = 'scheduled' AND reminder_status = 'sending'
                """,
                (self._now(),),
            )

    async def create(
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
        async with self.pool.connection() as connection:
            for _ in range(self.ID_ATTEMPTS):
                lecture_id = uuid.uuid4().hex[: self.ID_LENGTH]
                cursor = await connection.execute(
                    """
                    INSERT INTO lectures
                        (lecture_id, guild_id, title, lecturer, description,
                         starts_at, announce_channel_id, reminder_lead_minutes,
                         reminder_due_at, status, reminder_status, created_by,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'scheduled',
                            'pending', %s, %s, %s)
                    ON CONFLICT (lecture_id) DO NOTHING
                    RETURNING *
                    """,
                    (
                        lecture_id,
                        str(guild_id),
                        title,
                        lecturer,
                        description,
                        starts_at,
                        str(announce_channel_id),
                        reminder_lead_minutes,
                        starts_at - timedelta(minutes=reminder_lead_minutes),
                        None if created_by is None else str(created_by),
                        now,
                        now,
                    ),
                )
                row = await cursor.fetchone()
                if row is not None:
                    return row
        raise RuntimeError("failed to allocate a unique lecture id")

    async def get(self, lecture_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM lectures WHERE lecture_id = %s",
                (lecture_id,),
            )
            return await cursor.fetchone()

    async def list_lectures(
        self,
        guild_id: int,
        include_cancelled: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM lectures WHERE guild_id = %s"
        parameters: list[Any] = [str(guild_id)]
        if not include_cancelled:
            query += " AND status != 'cancelled'"
        query += " ORDER BY starts_at"
        async with self.pool.connection() as connection:
            cursor = await connection.execute(query, parameters)
            return await cursor.fetchall()

    async def update(
        self,
        lecture_id: str,
        title: str | None = None,
        lecturer: str | None = None,
        description: str | None = None,
        starts_at: datetime | None = None,
        announce_channel_id: int | None = None,
        reminder_lead_minutes: int | None = None,
    ) -> dict[str, Any] | None:
        current = await self.get(lecture_id)
        if current is None:
            return None

        assignments: list[str] = []
        values: list[Any] = []

        def set_field(column: str, value: Any) -> None:
            assignments.append(f"{column} = %s")
            values.append(value)

        if title is not None:
            set_field("title", title)
        if lecturer is not None:
            set_field("lecturer", lecturer)
        if description is not None:
            set_field("description", description)
        if announce_channel_id is not None:
            set_field("announce_channel_id", str(announce_channel_id))

        lead = (
            reminder_lead_minutes
            if reminder_lead_minutes is not None
            else current["reminder_lead_minutes"]
        )
        if reminder_lead_minutes is not None:
            set_field("reminder_lead_minutes", reminder_lead_minutes)
        if starts_at is not None or reminder_lead_minutes is not None:
            effective_start = starts_at or current["starts_at"]
            set_field(
                "reminder_due_at",
                effective_start.astimezone(timezone.utc) - timedelta(minutes=lead),
            )
        if starts_at is not None:
            set_field("starts_at", starts_at.astimezone(timezone.utc))

        if not assignments:
            return current

        set_field("updated_at", self._now())
        values.append(lecture_id)
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                f"UPDATE lectures SET {', '.join(assignments)} "
                "WHERE lecture_id = %s RETURNING *",
                values,
            )
            return await cursor.fetchone()

    async def cancel(self, lecture_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE lectures
                SET status = 'cancelled',
                    reminder_status = CASE
                        WHEN reminder_status IN ('pending', 'sending') THEN 'cancelled'
                        ELSE reminder_status
                    END,
                    updated_at = %s
                WHERE lecture_id = %s
                RETURNING *
                """,
                (self._now(), lecture_id),
            )
            return await cursor.fetchone()

    async def delete(self, lecture_id: str) -> bool:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "DELETE FROM lectures WHERE lecture_id = %s",
                (lecture_id,),
            )
            return cursor.rowcount > 0

    async def count_purgeable(self, before: datetime) -> int:
        """Count finished lectures last updated before ``before``."""
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT count(*) AS count FROM lectures
                WHERE (status = 'cancelled'
                       OR reminder_status IN ('sent', 'expired', 'cancelled'))
                  AND updated_at < %s
                """,
                (before.astimezone(timezone.utc),),
            )
            row = await cursor.fetchone()
            return int(row["count"])

    async def purge_old(self, before: datetime) -> int:
        """Delete finished lectures last updated before ``before``.

        A lecture is finished once it is cancelled or its reminder reached a
        terminal state (``sent``/``expired``/``cancelled``), so scheduled lectures
        still waiting to be announced are never removed.
        """
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                DELETE FROM lectures
                WHERE (status = 'cancelled'
                       OR reminder_status IN ('sent', 'expired', 'cancelled'))
                  AND updated_at < %s
                """,
                (before.astimezone(timezone.utc),),
            )
            return cursor.rowcount

    async def set_announcement_message(self, lecture_id: str, message_id: int) -> None:
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE lectures
                SET announcement_message_id = %s, updated_at = %s
                WHERE lecture_id = %s
                """,
                (str(message_id), self._now(), lecture_id),
            )

    async def claim_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Claim lecture reminders that are due, expiring ones that already started."""
        now = (now or self._now()).astimezone(timezone.utc)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                # A reminder only makes sense before the lecture starts; anything
                # that already began (e.g. while restarting) is expired instead.
                await connection.execute(
                    """
                    UPDATE lectures
                    SET reminder_status = 'expired', updated_at = %s
                    WHERE status = 'scheduled'
                      AND reminder_status IN ('pending', 'sending')
                      AND starts_at <= %s
                    """,
                    (now, now),
                )
                cursor = await connection.execute(
                    """
                    SELECT * FROM lectures
                    WHERE status = 'scheduled'
                      AND reminder_status = 'pending'
                      AND reminder_due_at <= %s
                      AND (next_attempt_at IS NULL OR next_attempt_at <= %s)
                    ORDER BY reminder_due_at
                    FOR UPDATE SKIP LOCKED
                    """,
                    (now, now),
                )
                rows = await cursor.fetchall()
                if rows:
                    await cursor.executemany(
                        """
                        UPDATE lectures
                        SET reminder_status = 'sending', updated_at = %s
                        WHERE lecture_id = %s AND reminder_status = 'pending'
                        """,
                        [(now, row["lecture_id"]) for row in rows],
                    )
                return rows

    async def mark_reminder_sent(self, lecture_id: str, message_id: int) -> None:
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'sent', reminder_message_id = %s,
                    reminder_sent_at = %s, last_error = NULL, updated_at = %s
                WHERE lecture_id = %s AND reminder_status = 'sending'
                """,
                (str(message_id), now, now, lecture_id),
            )

    async def release_reminder(
        self,
        lecture_id: str,
        error: str,
        retry_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> None:
        """Return a failed reminder to pending so it is retried before the start."""
        now = (now or self._now()).astimezone(timezone.utc)
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'pending',
                    reminder_attempts = reminder_attempts + 1,
                    next_attempt_at = %s,
                    last_error = %s,
                    updated_at = %s
                WHERE lecture_id = %s AND reminder_status = 'sending'
                """,
                (
                    now + timedelta(seconds=retry_seconds),
                    (error or "")[:500],
                    now,
                    lecture_id,
                ),
            )

    async def expire_reminder(self, lecture_id: str, error: str) -> None:
        """Give up on a reminder that cannot be delivered (permanent failure)."""
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE lectures
                SET reminder_status = 'expired',
                    reminder_attempts = reminder_attempts + 1,
                    next_attempt_at = NULL,
                    last_error = %s,
                    updated_at = %s
                WHERE lecture_id = %s AND reminder_status = 'sending'
                """,
                ((error or "")[:500], self._now(), lecture_id),
            )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


class TodoDatabase:
    """PostgreSQL access layer for the TodoTodo integration (Phase 5).

    Maps ticket channels to their TodoTodo items and tracks which moderators
    have recorded a decision. Both tables cascade from ``ticket_reminders``,
    so ``tools.purge`` removes finished tickets together with their items.
    """

    def __init__(
        self,
        pool: AsyncConnectionPool,
        retry_base_seconds: float = 60.0,
        retry_max_seconds: float = 21600.0,
        retry_max_attempts: int = 6,
    ) -> None:
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive.")
        if retry_max_attempts <= 0:
            raise ValueError("retry_max_attempts must be positive.")
        self.pool = pool
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.retry_max_attempts = retry_max_attempts

    async def ensure_schema(self) -> None:
        """Create the schema without touching any stored state."""
        async with self.pool.connection() as connection:
            await _create_schema(connection, self._now())

    async def initialize(self) -> None:
        async with self.pool.connection() as connection:
            await _create_schema(connection, self._now())

    async def register_todo(self, channel_id: int) -> bool:
        """Reserve the TodoTodo item slot for a channel (exactly once)."""
        now = self._now()
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO ticket_todos (channel_id, status, updated_at)
                VALUES (%s, 'pending', %s)
                ON CONFLICT (channel_id) DO NOTHING
                """,
                (str(channel_id), now),
            )
            return cursor.rowcount > 0

    async def claim_due_registrations(
        self, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Select pending TodoTodo registrations whose retry delay has passed."""
        now = (now or self._now()).astimezone(timezone.utc)
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT t.channel_id AS channel_id,
                       t.attempts AS attempts,
                       r.channel_name AS channel_name,
                       r.guild_id AS guild_id
                FROM ticket_todos t
                JOIN ticket_reminders r USING (channel_id)
                WHERE t.status = 'pending'
                  AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= %s)
                ORDER BY t.updated_at
                FOR UPDATE OF t SKIP LOCKED
                """,
                (now,),
            )
            return await cursor.fetchall()

    async def mark_todo_registered(self, channel_id: str, todo_id: str) -> None:
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE ticket_todos
                SET todo_id = %s, status = 'registered', last_error = NULL,
                    updated_at = %s
                WHERE channel_id = %s
                """,
                (todo_id, now, str(channel_id)),
            )

    async def fail_todo_registration(
        self,
        channel_id: str,
        error: str,
        now: datetime | None = None,
    ) -> tuple[str, int]:
        """Record a failed TodoTodo registration attempt with backoff."""
        now = (now or self._now()).astimezone(timezone.utc)
        message = (error or "")[:500]
        async with self.pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT attempts FROM ticket_todos
                    WHERE channel_id = %s AND status = 'pending'
                    FOR UPDATE
                    """,
                    (str(channel_id),),
                )
                row = await cursor.fetchone()
                if row is None:
                    return ("unknown", 0)
                attempts = row["attempts"] + 1
                if attempts >= self.retry_max_attempts:
                    status = "error"
                    next_attempt_at = None
                else:
                    status = "pending"
                    delay = min(
                        self.retry_max_seconds,
                        self.retry_base_seconds * (2 ** (attempts - 1)),
                    )
                    next_attempt_at = now + timedelta(seconds=delay)
                await connection.execute(
                    """
                    UPDATE ticket_todos
                    SET attempts = %s, next_attempt_at = %s, last_error = %s,
                        updated_at = %s
                    WHERE channel_id = %s
                    """,
                    (attempts, next_attempt_at, message, now, str(channel_id)),
                )
                return (status, attempts)

    async def get_todo(self, channel_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT * FROM ticket_todos WHERE channel_id = %s",
                (str(channel_id),),
            )
            return await cursor.fetchone()

    async def mark_todo_decided(self, channel_id: str) -> None:
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                UPDATE ticket_todos
                SET decided = TRUE, updated_at = %s
                WHERE channel_id = %s
                """,
                (now, str(channel_id)),
            )

    async def get_decision(
        self,
        channel_id: str,
        user_id: int | str,
    ) -> tuple[str | None, int]:
        """Return ``(previous_decision, judge_count)`` for one moderator.

        ``previous_decision`` is ``None`` when the moderator has not decided
        yet; ``judge_count`` is the number of distinct moderators who have
        decided for the channel.
        """
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT d.decision AS decision,
                       (SELECT count(*) FROM ticket_todo_decisions
                        WHERE channel_id = %s) AS judge_count
                FROM ticket_todo_decisions d
                WHERE d.channel_id = %s AND d.user_id = %s
                """,
                (str(channel_id), str(channel_id), str(user_id)),
            )
            row = await cursor.fetchone()
            if row is None:
                return (None, 0)
            return (row["decision"], int(row["judge_count"]))

    async def record_decision(
        self,
        channel_id: str,
        user_id: int | str,
        decision: str,
    ) -> int:
        """Store one moderator's decision and return the judge count.

        A moderator recording again replaces their previous decision
        (last-write-wins); the returned count is the number of distinct
        moderators who have decided for the channel.
        """
        now = self._now()
        async with self.pool.connection() as connection:
            await connection.execute(
                """
                INSERT INTO ticket_todo_decisions
                    (channel_id, user_id, decision, updated_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (channel_id, user_id) DO UPDATE
                SET decision = EXCLUDED.decision,
                    updated_at = EXCLUDED.updated_at
                """,
                (str(channel_id), str(user_id), decision, now),
            )
            cursor = await connection.execute(
                """
                SELECT count(*) AS count FROM ticket_todo_decisions
                WHERE channel_id = %s
                """,
                (str(channel_id),),
            )
            row = await cursor.fetchone()
            return int(row["count"])

    async def decision_tallies(self, channel_id: str) -> dict[str, int]:
        """Return per-decision vote counts (approval/reject) for the channel."""
        async with self.pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT decision, count(*) AS count
                FROM ticket_todo_decisions
                WHERE channel_id = %s
                GROUP BY decision
                """,
                (str(channel_id),),
            )
            rows = await cursor.fetchall()
            return {row["decision"]: int(row["count"]) for row in rows}
