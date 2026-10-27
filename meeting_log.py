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

import aiohttp
import discord
from discord import app_commands

from config import Config
from database import MeetingLogDatabase
from embeds import COLOR_INFO, MAX_DESCRIPTION, error_embed, truncate

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


class GeminiError(Exception):
    """Raised when the Gemini API call or its output cannot be used."""


# Gemini structured output schemas (design doc §8).
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "topics": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "summary": {"type": "STRING"},
                },
                "required": ["title", "summary"],
            },
        },
        "decisions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "todos": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["topics", "decisions", "todos"],
}

MERGE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "similar": {"type": "BOOLEAN"},
        "topics": EXTRACTION_SCHEMA["properties"]["topics"],
        "decisions": EXTRACTION_SCHEMA["properties"]["decisions"],
        "todos": EXTRACTION_SCHEMA["properties"]["todos"],
    },
    "required": ["similar", "topics", "decisions", "todos"],
}


def has_meeting_log_permission(
    is_administrator: bool,
    member_role_ids: set[int],
    required_role_ids: tuple[int, ...],
) -> bool:
    """Mirror the todo/lecture permission rule for ``/meeting-log``."""
    if is_administrator:
        return True
    if not required_role_ids:
        return False
    return bool(member_role_ids.intersection(required_role_ids))


_GEMINI_COMMON_RULES = (
    "与えられたDiscordの会話から、雑談・冗談・ネタ投稿などの"
    "おふざけは議事録に含めない。\n"
    "決定・進行・作業・予定・依頼など、真面目な話題だけを抽出する。\n"
    "会話中の発言者が誰かの判断材料になるため、やることには"
    "担当者が分かれば含める。\n"
    "出力はXMLではなくJSONで、全体で500文字程度に収める。"
)


def build_single_prompt(messages_text: str, previous_summary: str | None) -> str:
    if previous_summary:
        previous_rules = (
            f"前回の議事録は次の通りです。\n```\n{previous_summary}\n```\n"
            "前回と似た話題の議事録を書くときは similar=true にして、"
            "前回の内容を繰り返さず続きとして書く。\n"
            "違う話題の議事録を書くときは similar=false にする。\n"
        )
    else:
        previous_rules = (
            "previous_summary がないため similar=false を返す。\n"
        )
    return (
        f"{_GEMINI_COMMON_RULES}\n{previous_rules}\n"
        "会話:\n"
        f"{messages_text}"
    )


def build_chunk_prompt(chunk_text: str) -> str:
    return (
        f"{_GEMINI_COMMON_RULES}\n"
        "これは長い会話の一部分(チャンク)である。similar判定はしなくてよい。\n"
        "この部分に含まれる話題だけを抽出する。\n\n"
        "会話:\n"
        f"{chunk_text}"
    )


def build_merge_prompt(
    previous_summary: str | None, chunk_results: list[dict[str, Any]]
) -> str:
    parts = [
        json.dumps(result, ensure_ascii=False) for result in chunk_results
    ]
    extraction_rules = (
        "チャンクごとの抽出結果は重複を統合し、話題を1つにまとめ、"
        "全体で500文字程度に収める。\n"
    )
    if previous_summary:
        previous_rules = (
            f"前回の議事録は次の通りです。\n```\n{previous_summary}\n```\n"
            "前回と似た話題の議事録を書くときは similar=true にして、"
            "前回の内容を繰り返さず続きとして書く。\n"
            "違う話題の議事録を書くときは similar=false にする。\n"
        )
    else:
        previous_rules = (
            "previous_summary がないため similar=false を返す。\n"
        )
    extraction_text = "\n".join(parts)
    return (
        "長い会話をチャンクごとに抽出した結果が次のJSONです。\n"
        f"{extraction_text}\n\n"
        f"{extraction_rules}{previous_rules}"
    )


def parse_gemini_response(data: Any, require_similar: bool) -> dict[str, Any]:
    """Normalize a Gemini JSON payload into the meeting log shape."""
    if not isinstance(data, dict):
        raise GeminiError(
            f"Gemini response is not an object: {type(data).__name__}"
        )
    similar: bool | None = None
    if require_similar or "similar" in data:
        if not isinstance(data.get("similar"), bool):
            raise GeminiError("Gemini response has an invalid similar flag.")
        similar = data["similar"]
    raw_topics = data.get("topics")
    if not isinstance(raw_topics, list):
        raise GeminiError("Gemini response has invalid topics.")
    topics: list[dict[str, str]] = []
    for topic in raw_topics:
        if not isinstance(topic, dict):
            raise GeminiError("Gemini response has an invalid topic entry.")
        title = topic.get("title")
        summary = topic.get("summary")
        if not isinstance(title, str) or not isinstance(summary, str):
            raise GeminiError(
                "Gemini response has an invalid topic entry."
            )
        topics.append({"title": title, "summary": summary})
    parsed: dict[str, Any] = {"similar": similar, "topics": topics}
    for key in ("decisions", "todos"):
        values = data.get(key)
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise GeminiError(f"Gemini response has invalid {key}.")
        parsed[key] = values
    return parsed


def is_empty_extraction(result: dict[str, Any]) -> bool:
    return not (result["topics"] or result["decisions"] or result["todos"])


def build_meeting_log_embed(
    result: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    guild_id: int | None,
    channel_id: int | None,
    previous_message_id: int | None,
) -> discord.Embed:
    """Compose the public meeting log embed (JST times, capped description)."""
    lines = [
        f"対象期間: {window_start.astimezone(JST):%Y/%m/%d %H:%M}"
        f"〜{window_end.astimezone(JST):%Y/%m/%d %H:%M}",
        "",
    ]
    for topic in result["topics"]:
        lines.append(f"### {topic['title']}")
        lines.append(topic["summary"])
        lines.append("")
    lines.append("**決定事項・やること**")
    points = result["decisions"] + result["todos"]
    lines.extend(f"- {point}" for point in points)
    if (
        result.get("similar")
        and previous_message_id is not None
        and guild_id is not None
        and channel_id is not None
    ):
        lines.append(
            f"前回の議事録: "
            f"https://discord.com/channels/{guild_id}/{channel_id}/{previous_message_id}"
        )
    description = truncate("\n".join(lines).strip(), MAX_DESCRIPTION)
    return discord.Embed(title="議事録", description=description, color=COLOR_INFO)


def build_summary_text(result: dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)
