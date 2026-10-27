"""議事録作成コマンド ``/meeting-log`` (Phase 6).

Gemini API に過去24時間の会話を渡し、おふざけ(雑談・冗談・ネタ投稿)を
除外して真面目な話題だけを抽出した議事録を生成する。APIキーは
``x-goog-api-key`` ヘッダで送り、ログには絶対に出力しない。
"""

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger("rga_assistant")

# 収集対象の基本窓 (前回議事録が24時間以内なら差分のみ)。
WINDOW = timedelta(hours=24)
COLLECTION_LIMIT = 1000
MESSAGE_BODY_LIMIT = 500

# 大量メッセージ時の分割統合 (設計 §7): 全文がこのしきい値を超えると
# メッセージ単位でチャンクに分け、1チャンク目標 CHUNK_TARGET_CHARS。
CHUNK_SPLIT_THRESHOLD = 25000
CHUNK_TARGET_CHARS = 20000

# Bot 内の JST 固定 (main.py と同じ扱い)。
JST = timezone(timedelta(hours=9), name="JST")


def compute_since(latest: dict[str, Any] | None, now: datetime) -> datetime:
    """Return the timestamp from which messages should be collected.

    A log created within the last 24 hours narrows the window to its
    ``covered_until``; anything older (or absent) restarts the full window.
    psycopg returns aware datetimes, so no conversion is needed here.
    """
    if latest is None or now - latest["created_at"] >= WINDOW:
        return now - WINDOW
    return latest["covered_until"]


def collectable_entries(
    entries: Iterable[tuple[datetime, bool, str, str]],
) -> list[tuple[datetime, str, str]]:
    """Drop bot and blank messages and return the rest oldest first."""
    filtered = [
        (created_at, display_name, content)
        for created_at, is_bot, display_name, content in entries
        if not is_bot and content.strip()
    ]
    filtered.sort(key=lambda entry: entry[0])
    return filtered


def format_messages(entries: list[tuple[datetime, str, str]]) -> list[str]:
    """Format one prompt line per message (JST timestamp, display name, body).

    The body is cut to its first ``MESSAGE_BODY_LIMIT`` characters without an
    ellipsis: prompt lines feed the chunk size math, which counts exact
    characters.
    """
    return [
        f"[{created_at.astimezone(JST).strftime('%Y/%m/%d %H:%M')}] "
        f"{display_name}: {content[:MESSAGE_BODY_LIMIT]}"
        for created_at, display_name, content in entries
    ]


def split_into_chunks(lines: list[str]) -> list[str]:
    """Split prompt lines into chunks of about ``CHUNK_TARGET_CHARS``.

    A line is never cut in half: a chunk is closed once it reaches the
    target length, so the overrun is bounded by a single message.
    """
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in lines:
        length += len(line) + (1 if current else 0)
        current.append(line)
        if length >= CHUNK_TARGET_CHARS:
            chunks.append("\n".join(current))
            current = []
            length = 0
    if current:
        chunks.append("\n".join(current))
    return chunks
