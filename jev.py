"""Jev (TypeSafe AI judgment model) client for ticket application status.

The API accepts text / JSON / text-array state only — images cannot be sent,
so attachment metadata (``content_type``) is passed instead of the files
themselves. See https://thejevai.com/docs for the request/response shape.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger("rga_assistant")

JEV_ENDPOINT = "https://thejevai.com/v1/systemone"
DEFAULT_JEV_MODEL = "typesafe/jev-1.14"

# The endpoint caps requests at 32 KiB, so truncate long contents.
MAX_STATE_MESSAGES = 50
MAX_STATE_CONTENT_LENGTH = 500

# Possible values of the application_state question (also stored in the DB).
APPLICATION_STATE_CHOICES = ("未申請", "申請中", "審査中", "判断不能")
UNKNOWN_STATE = "判断不能"


class JevError(Exception):
    """Raised when the Jev API cannot provide a usable answer."""


@dataclass(frozen=True)
class JevAssessment:
    application_state: str
    confidence: float | None
    no_application_probability: float | None


def build_channel_state(
    messages: list[dict[str, Any]],
    max_messages: int = MAX_STATE_MESSAGES,
) -> list[dict[str, Any]]:
    """Convert a ticket channel history into Jev-ready JSON state.

    ``messages`` is ordered oldest first; each item has ``author``, ``content``
    and ``attachments`` (list of dicts with at least ``content_type``). Images
    themselves are never sent — only their MIME metadata.
    """
    state = []
    for message in messages[-max_messages:]:
        author = str(message.get("author") or "")
        content = (str(message.get("content") or ""))[:MAX_STATE_CONTENT_LENGTH]
        attachments = [
            {"content_type": attachment.get("content_type")}
            for attachment in (message.get("attachments") or [])
        ]
        item: dict[str, Any] = {"author": author, "content": content}
        if attachments:
            item["attachments"] = attachments
        state.append(item)
    return state


def build_questions() -> dict[str, Any]:
    """Build the application status questions sent to Jev."""
    return {
        "application_state": {
            "type": "choice",
            "instructions": (
                "Discordのチケットチャンネルの投稿履歴から、"
                "このチケットの申請状況を1つ選んでください。"
            ),
            "criteria": {
                "未申請": "まだ誰も申請をしていない状態",
                "申請中": "申請が提出され、対応待ちの状態",
                "審査中": "申請の内容が確認・審査されている状態",
                "判断不能": "履歴から状況を判断できない状態",
            },
        },
        "no_application_yet": {
            "type": "noul",
            "instructions": "このチケットではまだ誰も申請していないか。",
        },
    }


def parse_answer(data: dict[str, Any]) -> JevAssessment:
    """Extract the application status from one Jev API response."""
    result = (data or {}).get("data", {}).get("result", {})
    answers = result.get("answers") or {}

    state_answer = answers.get("application_state") or {}
    application_state = state_answer.get("choice")
    if application_state not in APPLICATION_STATE_CHOICES:
        raise JevError(
            f"Jev returned an unexpected application state: {application_state!r}"
        )
    if application_state == UNKNOWN_STATE:
        raise JevError("Jev could not judge the application state.")

    no_application_answer = answers.get("no_application_yet") or {}
    no_application = no_application_answer.get("noul")
    if not isinstance(no_application, (int, float)):
        raise JevError("Jev returned no noul probability for the question.")

    confidence = state_answer.get("confidence")
    if not isinstance(confidence, (int, float)):
        confidence = None
    return JevAssessment(
        application_state=application_state,
        confidence=confidence,
        no_application_probability=float(no_application),
    )


async def evaluate_application_status(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    state: Any,
    timeout_seconds: float = 30.0,
) -> JevAssessment:
    """Call the Jev API once and return the parsed application assessment."""
    payload = {
        "model": model,
        "state": state,
        "questions": build_questions(),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(
            JEV_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            text = await response.text()
            if response.status >= 400:
                logger.warning(
                    "Jev API request failed: status=%s body=%s",
                    response.status,
                    text[:200],
                )
                raise JevError(f"Jev API returned HTTP {response.status}.")
            data = await response.json(content_type=None)
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        raise JevError(f"Jev API communication failed: {exc}") from exc
    return parse_answer(data)


def assessment_to_status(assessment: JevAssessment) -> tuple[bool | None, str]:
    """Map a Jev assessment to ``(has_application, detail)`` for the database.

    ``no_application_yet`` (true probability that nobody applied yet) decides
    the boolean; the choice state is kept as the human-readable detail.
    """
    probability = assessment.no_application_probability
    if probability is None:
        return None, assessment.application_state
    has_application = probability < 0.5
    return has_application, assessment.application_state
