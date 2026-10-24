"""Ticket application status detection (simple / jev modes).

Issue #5:judge whether anyone has already applied in a ticket channel and show
the result on the operations reminder embed. Judgment records live in the
``application_status`` table, independent from the reminder lifecycle (they are
kept for sent/cancelled tickets too).

- ``simple`` (default): free, event-driven local detection. An image file
  attachment (MIME ``image/*``) posted by a user counts as application
  evidence. No external API is involved.
- ``jev``: sends a trimmed channel history (text + attachment *metadata* only;
  Jev cannot accept image input itself) to the Jev System One API and stores a
  multi-stage judgment (未申請/申請中/審査中/判断不能) with confidence.
  Failures fall back to the simple criteria and are logged explicitly.

Pure helpers (state building, payload trimming, response parsing, embed field
formatting) live here next to the small I/O pieces, mirroring ``lecture.py``.
"""

import asyncio
import json
import logging
from typing import Any, Callable, Iterator

import aiohttp
import discord
from discord.ext import tasks

SIMPLE_MODE = "simple"
JEV_MODE = "jev"

# Jev API reference (https://thejevai.com/docs): one state + typed questions,
# at most 8 questions and 32 KiB of JSON per request. 429/529 are retried with
# exponential backoff; everything else is a permanent failure for this client.
JEV_ENDPOINT = "https://thejevai.com/v1/systemone"
JEV_MAX_PAYLOAD_BYTES = 32 * 1024
JEV_RETRY_ATTEMPTS = 3
JEV_RETRY_BASE_SECONDS = 2.0
JEV_RETRY_MAX_SECONDS = 60.0
JEV_TIMEOUT_SECONDS = 30.0

# History scan window and per-message caps keep the payload lean. Attachment
# *files* are never sent: Jev only receives their MIME metadata, so the simple
# image-evidence criteria remain the authoritative signal for images.
JEV_HISTORY_MESSAGE_LIMIT = 50
JEV_STATE_CONTENT_LIMIT = 2000

# Low-frequency Jev refresh (cost control: jev is never called per message).
# Every cycle issues one Jev call per monitored ticket channel, so the interval
# is kept generous and documented in README.
JEV_REFRESH_SECONDS = 6 * 60 * 60

APPLICATION_STATES = ("未申請", "申請中", "審査中", "判断不能")
JEV_CONFIDENCE_THRESHOLD_DEFAULT = 0.7

_LOW_CONFIDENCE_NOTE = "※ 人による確認を推奨"

logger = logging.getLogger("rga_assistant")


class JevError(Exception):
    """Permanent Jev failure; the caller should fall back to simple."""


class JevRetryableError(JevError):
    """Transient Jev failure (HTTP 429/529) worth backing off and retrying."""


def lean_message(message: Any) -> dict[str, Any]:
    """Reduce one message to the minimal state item Jev is allowed to see."""
    content = (message.content or "")[:JEV_STATE_CONTENT_LIMIT]
    attachments = [
        {
            "content_type": attachment.content_type,
            "name": attachment.filename,
            "size": attachment.size,
        }
        for attachment in message.attachments
    ]
    return {"author": "user", "content": content, "attachments": attachments}


async def fetch_lean_history(
    channel: discord.TextChannel,
    limit: int = JEV_HISTORY_MESSAGE_LIMIT,
) -> list[dict[str, Any]]:
    """Read recent user messages (bot/webhook posts excluded) oldest-first.

    Ticket Tool's own embedded panels are bot/webhook messages and are skipped,
    mirroring the cancel-criteria exclusion. Message Content Intent must be
    enabled or Discord strips ``message.content``.
    """
    lean: list[dict[str, Any]] = []
    async for message in channel.history(limit=limit):
        if message.author.bot or message.webhook_id is not None:
            continue
        item = lean_message(message)
        if not item["content"].strip() and not item["attachments"]:
            continue
        lean.append(item)
    lean.reverse()
    return lean


def history_has_image_attachment(lean_messages: list[dict[str, Any]]) -> bool:
    """True when any scanned history item carries an image file attachment."""
    return any(
        (attachment.get("content_type") or "").startswith("image/")
        for message in lean_messages
        for attachment in message.get("attachments", ())
    )


def build_state(channel_id: int, lean_messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"channel_id": str(channel_id), "messages": list(lean_messages)}


def build_request(state: dict[str, Any], model: str) -> dict[str, Any]:
    """Build the Jev request: one Choice for the state and one Noul for yes/no."""
    return {
        "state": state,
        "model": model,
        "questions": {
            "application_state": {
                "type": "choice",
                "instructions": "このチケットの申請状況は？",
                "criteria": {
                    "未申請": "まだ誰も申請していない",
                    "申請中": "申請は存在するがまだ審査中",
                    "審査中": "申請が受理され審査が進行中",
                    "判断不能": "会話だけでは判断できない",
                },
            },
            "no_application_yet": {
                "type": "noul",
                "instructions": "このチケットではまだ誰も申請していないか",
            },
        },
    }


def serialize_request(request: dict[str, Any]) -> bytes:
    """Serialize a request exactly the way it leaves for the API."""
    return json.dumps(
        request, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def build_request_within_limit(
    lean_messages: list[dict[str, Any]],
    channel_id: int,
    model: str,
    limit: int = JEV_MAX_PAYLOAD_BYTES,
) -> dict[str, Any] | None:
    """Build a request whose serialised payload fits the 32 KiB Jev limit.

    Oldest messages are dropped first. ``None`` means even the trimmed request
    would not fit (or nothing is left to judge); the caller then falls back to
    the simple criteria instead of guessing.
    """
    kept = list(lean_messages)
    trimmed = False
    while True:
        request = build_request(build_state(channel_id, kept), model)
        if len(serialize_request(request)) <= limit:
            if trimmed and not kept:
                # Every message was dropped and nothing is left to judge.
                return None
            return request
        kept = kept[1:]
        trimmed = True


def parse_jev_response(payload: Any) -> dict[str, Any]:
    """Parse ``data.result.answers`` into a normalized judgment.

    ``application_state`` (Choice) is the primary signal; the Noul answer is
    auxiliary and only stored for debugging. Anything unexpected raises
    :class:`JevError` so the caller records the failure and falls back.
    """
    try:
        answers = payload["data"]["result"]["answers"]
        choice = answers["application_state"]
        state = choice["choice"]
        confidence = float(choice["confidence"])
        probabilities = choice["probabilities"]
        noul = answers.get("no_application_yet")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise JevError(f"unexpected Jev response structure: {exc!r}") from exc
    if not isinstance(state, str) or not isinstance(probabilities, dict):
        raise JevError("Jev answered without a choice or probabilities.")
    if state not in APPLICATION_STATES:
        raise JevError(f"Jev answered with an unknown state: {state}")
    result: dict[str, Any] = {
        "application_state": state,
        "confidence": confidence,
    }
    if isinstance(noul, dict) and isinstance(noul.get("noul"), (int, float)):
        result["no_application_yet"] = float(noul["noul"])
    return result


def jev_result_detail(parsed: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "application_state": parsed["application_state"],
        "confidence": parsed["confidence"],
    }
    if "no_application_yet" in parsed:
        detail["no_application_yet"] = parsed["no_application_yet"]
    return detail


def has_application_from_jev(
    parsed: dict[str, Any], confidence_threshold: float
) -> bool | None:
    """Map a Jev judgment to the boolean DB record (see the module docstring).

    ``None`` means no decision: 判断不能, or a low-confidence 未申請 which is
    reference-only and must not cancel out human checks.
    """
    state = parsed["application_state"]
    if state == "判断不能":
        return None
    if state == "未申請":
        if parsed["confidence"] >= confidence_threshold:
            return False
        return None
    return True


def build_application_status_field(
    status: dict[str, Any] | None,
    configured_mode: str,
    confidence_threshold: float,
) -> str:
    """Format the ``申請の確認`` field value for the operations embed.

    - simple row / no row: 添付画像検知 basis (never guaranteed by itself).
    - jev row: state + confidence; below the threshold (or 判断不能) it is
      explicitly marked as a reference value for human review.
    - When ``jev`` is configured but the latest row came from the simple
      criteria, the value is labelled as the fallback.
    """
    if status is not None and status.get("mode") == JEV_MODE:
        detail = status.get("detail") or {}
        state = str(detail.get("application_state") or "判断不能")
        confidence = detail.get("confidence")
        basis = (
            f"信頼 {confidence:.2f} / Jev"
            if isinstance(confidence, (int, float))
            else "信頼 - / Jev"
        )
        if state == "判断不能":
            return f"判断不能（{basis}）{_LOW_CONFIDENCE_NOTE}"
        if confidence is None or confidence < confidence_threshold:
            return (
                f"参考: {state}の可能性（{basis}）{_LOW_CONFIDENCE_NOTE}"
            )
        return f"{state}（{basis}）"

    has_evidence = status is not None and bool(status.get("has_application"))
    if configured_mode == JEV_MODE:
        basis = "simple・フォールバック"
    else:
        basis = "添付画像検知"
    return f"あり（{basis}）" if has_evidence else f"なし（{basis}）"


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


class JevApplicationJudge:
    """I/O-only client for the Jev System One endpoint.

    The API key never appears in logs: only status codes and elapsed times are
    logged, and network errors surface as :class:`JevError` without bodies.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = JEV_TIMEOUT_SECONDS,
        retry_attempts: int = JEV_RETRY_ATTEMPTS,
        retry_base_seconds: float = JEV_RETRY_BASE_SECONDS,
        retry_max_seconds: float = JEV_RETRY_MAX_SECONDS,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        if retry_attempts <= 0:
            raise ValueError("retry_attempts must be positive.")
        self._retry_attempts = retry_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    def backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff for HTTP 429/529: base × 2^(attempt-1), capped."""
        return min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2 ** (attempt - 1)),
        )

    async def judge(
        self, session: aiohttp.ClientSession, request: dict[str, Any]
    ) -> dict[str, Any]:
        """POST one request and return the parsed judgment.

        Only HTTP 429/529 are retried with backoff; timeouts, network errors,
        other status codes, and malformed answers are permanent
        (:class:`JevError`) so the scheduler falls back to simple immediately.
        """
        for attempt in range(1, self._retry_attempts + 1):
            body, status = await self._post(session, request)
            if status in (429, 529):
                error = JevRetryableError(f"Jev API returned HTTP {status}")
                if attempt >= self._retry_attempts:
                    raise error
                await asyncio.sleep(self.backoff_seconds(attempt))
                logger.warning(
                    "Retrying Jev request after backoff: status=%s attempt=%s/%s",
                    status,
                    attempt,
                    self._retry_attempts,
                )
                continue
            if status != 200:
                raise JevError(f"Jev API returned HTTP {status}")
            return parse_jev_response(body)
        raise JevError("Jev retry loop exited unexpectedly")

    async def _post(
        self, session: aiohttp.ClientSession, request: dict[str, Any]
    ) -> tuple[Any, int]:
        try:
            async with session.post(
                JEV_ENDPOINT,
                json=request,
                headers=_auth_headers(self._api_key),
                timeout=self._timeout,
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = None
                return body, response.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            # aiohttp wraps most socket-level problems in ClientError, but a
            # bare ConnectionError/OSError can escape; treat all as permanent.
            raise JevError(f"Jev request failed: {exc!r}") from exc


class JevApplicationScheduler:
    """Periodically re-judges monitored ticket channels with Jev (jev mode).

    Because Jev charges per request, judgments run at a low fixed frequency
    (never per message). The results are upserted into ``application_status``
    so restarts keep them and the operations embed always shows the latest
    stored judgment.
    """

    def __init__(
        self,
        client: discord.Client,
        database: Any,
        judge: JevApplicationJudge,
        session: aiohttp.ClientSession,
        *,
        confidence_threshold: float = JEV_CONFIDENCE_THRESHOLD_DEFAULT,
        is_monitored_channel: Callable[[discord.TextChannel], bool],
    ) -> None:
        self.client = client
        self.database = database
        self.judge = judge
        self.session = session
        self.confidence_threshold = confidence_threshold
        self._is_monitored_channel = is_monitored_channel

    def start(self) -> None:
        self.judge_loop.start()

    @tasks.loop(seconds=JEV_REFRESH_SECONDS)
    async def judge_loop(self) -> None:
        for channel in self._iter_monitored_channels():
            try:
                await self._judge_channel(channel)
            except Exception:  # keep the loop alive on unexpected errors
                logger.exception(
                    "Unexpected error while judging channel %s", channel.id
                )

    @judge_loop.before_loop
    async def before_judge_loop(self) -> None:
        await self.client.wait_until_ready()

    def _iter_monitored_channels(self) -> Iterator[discord.TextChannel]:
        for guild in self.client.guilds:
            for channel in guild.text_channels:
                if self._is_monitored_channel(channel):
                    yield channel

    async def _judge_channel(self, channel: discord.TextChannel) -> None:
        try:
            history = await fetch_lean_history(channel)
        except discord.HTTPException as exc:
            logger.warning(
                "Failed to read history for application judgment: "
                "channel_id=%s error=%s",
                channel.id,
                exc,
            )
            return

        if not history:
            # Nothing a user has said yet: a fresh ticket channel would only
            # earn an empty judgment at Jev's cost, so skip it.
            logger.debug(
                "Skipping Jev judgment; no user messages yet: channel_id=%s",
                channel.id,
            )
            return

        request = build_request_within_limit(history, channel.id, self.judge.model)
        if request is None:
            logger.warning(
                "History exceeds the Jev payload limit; falling back to the "
                "simple criteria: channel_id=%s",
                channel.id,
            )
            await self._save_simple_fallback(channel.id, history)
            return

        try:
            parsed = await self.judge.judge(self.session, request)
        except JevError as exc:
            logger.warning(
                "Jev judgment unavailable; falling back to the simple criteria "
                "(FALLBACK): channel_id=%s error=%s",
                channel.id,
                exc,
            )
            await self._save_simple_fallback(channel.id, history)
            return

        has_application = has_application_from_jev(
            parsed, self.confidence_threshold
        )
        await self.database.save(
            channel.id,
            has_application=has_application,
            mode=JEV_MODE,
            detail=jev_result_detail(parsed),
        )
        logger.info(
            "Jev judged channel: channel_id=%s state=%s confidence=%s "
            "has_application=%s",
            channel.id,
            parsed["application_state"],
            parsed["confidence"],
            has_application,
        )

    async def _save_simple_fallback(
        self, channel_id: int, history: list[dict[str, Any]]
    ) -> None:
        """Record the simple criteria only when they find positive evidence.

        Writing nothing on "no image found" keeps an older *strong* judgment
        (for example a previous Jev あり) from being silently downgraded by a
        temporary API outage.
        """
        if not history_has_image_attachment(history):
            return
        await self.database.save(
            channel_id,
            has_application=True,
            mode=SIMPLE_MODE,
            detail=None,
        )
