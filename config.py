import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
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


@dataclass(frozen=True)
class Config:
    discord_token: str
    ticket_category_ids: Tuple[int, ...]
    excluded_channel_ids: Tuple[int, ...]
    reminder_trigger_text: str
    reminder_after: timedelta
    database_path: Path


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

    database_path = Path(os.getenv("RGA_DATABASE_PATH", "data/rga_assistant.sqlite3"))
    return Config(
        discord_token=token,
        ticket_category_ids=category_ids,
        excluded_channel_ids=excluded_channel_ids,
        reminder_trigger_text=trigger_text,
        reminder_after=reminder_after,
        database_path=database_path,
    )
