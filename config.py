import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Tuple

from dotenv import load_dotenv


def parse_discord_ids(
    value: str,
    setting_name: str,
    allow_empty: bool = False,
) -> Tuple[int, ...]:
    if allow_empty and not value.strip():
        return ()

    try:
        ids = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",")))
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be comma-separated integers.") from exc

    if not ids or any(value <= 0 for value in ids):
        raise ValueError(f"{setting_name} must contain positive Discord IDs.")
    return ids


def parse_category_ids(value: str) -> Tuple[int, ...]:
    return parse_discord_ids(value, "Category IDs")


def parse_optional_id(value: str, setting_name: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be a Discord ID.") from exc
    if parsed <= 0:
        raise ValueError(f"{setting_name} must be a positive Discord ID.")
    return parsed


def parse_positive_float(value: str, setting_name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be a number.") from exc
    if parsed <= 0:
        raise ValueError(f"{setting_name} must be positive.")
    return parsed


def parse_positive_int(value: str, setting_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be an integer.") from exc
    if parsed <= 0:
        raise ValueError(f"{setting_name} must be positive.")
    return parsed


def validate_mention_targets(role_id: int | None, user_id: int | None) -> None:
    if role_id is not None and user_id is not None:
        raise ValueError(
            "Set either RGA_OPERATIONS_MENTION_ROLE_ID or "
            "RGA_OPERATIONS_MENTION_USER_ID, not both."
        )


REMINDER_UNITS_IN_SECONDS = {
    "seconds": 1,
    "minutes": 60,
    "hours": 60 * 60,
    "days": 24 * 60 * 60,
    "weeks": 7 * 24 * 60 * 60,
}


def parse_reminder_after(value: int, unit: str) -> timedelta:
    if value <= 0:
        raise ValueError("Reminder interval must be positive.")
    normalized_unit = unit.strip().lower()
    seconds_per_unit = REMINDER_UNITS_IN_SECONDS.get(normalized_unit)
    if seconds_per_unit is None:
        choices = ", ".join(REMINDER_UNITS_IN_SECONDS)
        raise ValueError(f"RGA_REMINDER_UNIT must be one of: {choices}.")
    return timedelta(seconds=value * seconds_per_unit)


REMINDER_TARGETS = {
    "ticket": ("ticket",),
    "operations": ("operations",),
    "both": ("ticket", "operations"),
}


def parse_reminder_targets(value: str) -> Tuple[str, ...]:
    normalized = value.strip().lower()
    targets = REMINDER_TARGETS.get(normalized)
    if targets is None:
        choices = ", ".join(REMINDER_TARGETS)
        raise ValueError(f"RGA_REMINDER_TARGET must be one of: {choices}.")
    return targets


@dataclass(frozen=True)
class Config:
    discord_token: str
    ticket_category_ids: Tuple[int, ...]
    excluded_channel_ids: Tuple[int, ...]
    reminder_trigger_text: str
    reminder_after: timedelta
    reminder_targets: Tuple[str, ...]
    operations_channel_id: int | None
    operations_mention_role_id: int | None
    operations_mention_user_id: int | None
    retry_base_seconds: float
    retry_max_seconds: float
    retry_max_attempts: int
    database_url: str
    lecture_role_ids: Tuple[int, ...]
    lecture_reminder_minutes: int
    lecture_announce_channel_id: int | None


def load_config() -> Config:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is required.")

    try:
        category_ids = parse_category_ids(
            os.getenv(
                "RGA_TICKET_CATEGORY_IDS",
                os.environ.get("RGA_TICKET_CATEGORY_ID", ""),
            )
        )
        excluded_channel_ids = parse_discord_ids(
            os.getenv("RGA_EXCLUDED_CHANNEL_IDS", ""),
            "RGA_EXCLUDED_CHANNEL_IDS",
            allow_empty=True,
        )
        reminder_value = int(
            os.getenv(
                "RGA_REMINDER_VALUE",
                os.getenv("RGA_REMINDER_DAYS", "14"),
            )
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "RGA_TICKET_CATEGORY_IDS (or legacy RGA_TICKET_CATEGORY_ID) and "
            "the reminder interval must contain valid values."
        ) from exc

    trigger_text = os.getenv("RGA_REMINDER_TRIGGER_TEXT", "").strip()
    if not trigger_text:
        raise ValueError("RGA_REMINDER_TRIGGER_TEXT is required.")

    if reminder_value <= 0:
        raise ValueError("RGA_REMINDER_VALUE must be positive.")

    try:
        reminder_after = parse_reminder_after(
            reminder_value,
            os.getenv("RGA_REMINDER_UNIT", "days"),
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    reminder_targets = parse_reminder_targets(
        os.getenv("RGA_REMINDER_TARGET", "ticket")
    )
    operations_channel_id = parse_optional_id(
        os.getenv("RGA_OPERATIONS_CHANNEL_ID", ""),
        "RGA_OPERATIONS_CHANNEL_ID",
    )
    if "operations" in reminder_targets and operations_channel_id is None:
        raise ValueError(
            "RGA_OPERATIONS_CHANNEL_ID is required when RGA_REMINDER_TARGET is "
            "operations or both."
        )
    operations_mention_role_id = parse_optional_id(
        os.getenv("RGA_OPERATIONS_MENTION_ROLE_ID", ""),
        "RGA_OPERATIONS_MENTION_ROLE_ID",
    )
    operations_mention_user_id = parse_optional_id(
        os.getenv("RGA_OPERATIONS_MENTION_USER_ID", ""),
        "RGA_OPERATIONS_MENTION_USER_ID",
    )
    validate_mention_targets(operations_mention_role_id, operations_mention_user_id)

    retry_base_seconds = parse_positive_float(
        os.getenv("RGA_RETRY_BASE_SECONDS", "60"),
        "RGA_RETRY_BASE_SECONDS",
    )
    retry_max_seconds = parse_positive_float(
        os.getenv("RGA_RETRY_MAX_SECONDS", "21600"),
        "RGA_RETRY_MAX_SECONDS",
    )
    retry_max_attempts = parse_positive_int(
        os.getenv("RGA_RETRY_MAX_ATTEMPTS", "6"),
        "RGA_RETRY_MAX_ATTEMPTS",
    )

    database_url = os.getenv("RGA_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError(
            "RGA_DATABASE_URL is required "
            "(例: postgresql://user:password@localhost:5432/rga)."
        )
    lecture_role_ids = parse_discord_ids(
        os.getenv("RGA_LECTURE_ROLE_IDS", ""),
        "RGA_LECTURE_ROLE_IDS",
        allow_empty=True,
    )
    lecture_reminder_minutes = parse_positive_int(
        os.getenv("RGA_LECTURE_REMINDER_MINUTES", "15"),
        "RGA_LECTURE_REMINDER_MINUTES",
    )
    lecture_announce_channel_id = parse_optional_id(
        os.getenv("RGA_LECTURE_ANNOUNCE_CHANNEL_ID", ""),
        "RGA_LECTURE_ANNOUNCE_CHANNEL_ID",
    )
    return Config(
        discord_token=token,
        ticket_category_ids=category_ids,
        excluded_channel_ids=excluded_channel_ids,
        reminder_trigger_text=trigger_text,
        reminder_after=reminder_after,
        reminder_targets=reminder_targets,
        operations_channel_id=operations_channel_id,
        operations_mention_role_id=operations_mention_role_id,
        operations_mention_user_id=operations_mention_user_id,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=retry_max_seconds,
        retry_max_attempts=retry_max_attempts,
        database_url=database_url,
        lecture_role_ids=lecture_role_ids,
        lecture_reminder_minutes=lecture_reminder_minutes,
        lecture_announce_channel_id=lecture_announce_channel_id,
    )
