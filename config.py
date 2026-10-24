import os
from dataclasses import dataclass
from datetime import timedelta

from dotenv import load_dotenv


ENV_FILE = ".env"
SETTING_FILE = ".setting"


def load_env_files(
    setting_file: str = SETTING_FILE,
    env_file: str = ENV_FILE,
) -> None:
    """Load deployment settings and secrets from their two files.

    Both files use the KEY=VALUE (dotenv) format: secrets such as the Discord
    token and the database URL live in ``.env``, deployment-specific settings
    live in ``.setting``. Values already present in the OS environment are
    never overridden, so a later file never clobbers an earlier one; this
    leaves systemd units or shells free to pass explicit variables.
    """
    load_dotenv(dotenv_path=setting_file)
    load_dotenv(dotenv_path=env_file)


def parse_discord_ids(
    value: str,
    setting_name: str,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    if allow_empty and not value.strip():
        return ()

    try:
        ids = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",")))
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be comma-separated integers.") from exc

    if not ids or any(value <= 0 for value in ids):
        raise ValueError(f"{setting_name} must contain positive Discord IDs.")
    return ids


def parse_category_ids(value: str) -> tuple[int, ...]:
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


def parse_reminder_targets(value: str) -> tuple[str, ...]:
    normalized = value.strip().lower()
    targets = REMINDER_TARGETS.get(normalized)
    if targets is None:
        choices = ", ".join(REMINDER_TARGETS)
        raise ValueError(f"RGA_REMINDER_TARGET must be one of: {choices}.")
    return targets


def parse_trigger_texts(value: str) -> tuple[str, ...]:
    """Parse the comma-separated cancel keywords, preserving order and deduping."""
    texts = tuple(
        dict.fromkeys(item.strip() for item in value.split(",") if item.strip())
    )
    if not texts:
        raise ValueError("RGA_REMINDER_TRIGGER_TEXT is required.")
    return texts


CANCEL_TRIGGERS = {
    "image": "image",
    "keyword": "keyword",
}


def parse_cancel_trigger(value: str) -> str:
    """Parse the ticket reminder cancellation condition (image or keyword)."""
    normalized = value.strip().lower()
    if normalized not in CANCEL_TRIGGERS:
        choices = ", ".join(CANCEL_TRIGGERS)
        raise ValueError(f"RGA_REMINDER_CANCEL_TRIGGER must be one of: {choices}.")
    return normalized


APPLICATION_STATUS_MODES = {
    "simple": "simple",
    "jev": "jev",
}


def parse_application_status_mode(value: str) -> str:
    """Parse the ticket application detection mode (simple or jev)."""
    normalized = value.strip().lower()
    if normalized not in APPLICATION_STATUS_MODES:
        choices = ", ".join(APPLICATION_STATUS_MODES)
        raise ValueError(
            f"RGA_APPLICATION_STATUS_MODE must be one of: {choices}."
        )
    return normalized


def parse_probability(value: str, setting_name: str) -> float:
    """Parse a probability-like setting in the closed interval [0.0, 1.0]."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{setting_name} must be a number.") from exc
    if parsed < 0.0 or parsed > 1.0:
        raise ValueError(f"{setting_name} must be between 0.0 and 1.0.")
    return parsed


@dataclass(frozen=True)
class Config:
    discord_token: str
    ticket_category_ids: tuple[int, ...]
    excluded_channel_ids: tuple[int, ...]
    cancel_trigger: str
    reminder_trigger_texts: tuple[str, ...]
    reminder_after: timedelta
    reminder_targets: tuple[str, ...]
    retention_days: int
    operations_channel_id: int | None
    operations_mention_role_id: int | None
    operations_mention_user_id: int | None
    retry_base_seconds: float
    retry_max_seconds: float
    retry_max_attempts: int
    database_url: str
    lecture_role_ids: tuple[int, ...]
    lecture_reminder_minutes: int
    lecture_announce_channel_id: int | None
    application_status_mode: str
    jev_api_key: str
    jev_model: str
    application_status_confidence_threshold: float


def load_config() -> Config:
    load_env_files()

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
    except ValueError as exc:
        raise ValueError(
            "RGA_TICKET_CATEGORY_IDS (or legacy RGA_TICKET_CATEGORY_ID) and "
            "the reminder interval must contain valid values."
        ) from exc

    cancel_trigger = parse_cancel_trigger(
        os.getenv("RGA_REMINDER_CANCEL_TRIGGER", "image")
    )
    trigger_texts: tuple[str, ...] = ()
    if cancel_trigger == "keyword":
        trigger_texts = parse_trigger_texts(
            os.getenv("RGA_REMINDER_TRIGGER_TEXT", "")
        )

    if reminder_value <= 0:
        raise ValueError("RGA_REMINDER_VALUE must be positive.")

    reminder_after = parse_reminder_after(
        reminder_value,
        os.getenv("RGA_REMINDER_UNIT", "days"),
    )

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
    retention_days = parse_positive_int(
        os.getenv("RGA_RETENTION_DAYS", "90"),
        "RGA_RETENTION_DAYS",
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

    # Application status detection (Issue #5). `simple` needs no external API;
    # `jev` fails fast at startup when its API key is missing, so a mistaken
    # configuration never silently degrades to an unconfigured service.
    application_status_mode = parse_application_status_mode(
        os.getenv("RGA_APPLICATION_STATUS_MODE", "simple")
    )
    jev_api_key = os.getenv("RGA_JEV_API_KEY", "").strip()
    if application_status_mode == "jev" and not jev_api_key:
        raise ValueError(
            "RGA_JEV_API_KEY is required when RGA_APPLICATION_STATUS_MODE is jev."
        )
    jev_model = os.getenv("RGA_JEV_MODEL", "").strip() or "typesafe/jev-1.14"
    application_status_confidence_threshold = parse_probability(
        os.getenv("RGA_APPLICATION_STATUS_CONFIDENCE_THRESHOLD", "0.7"),
        "RGA_APPLICATION_STATUS_CONFIDENCE_THRESHOLD",
    )

    return Config(
        discord_token=token,
        ticket_category_ids=category_ids,
        excluded_channel_ids=excluded_channel_ids,
        cancel_trigger=cancel_trigger,
        reminder_trigger_texts=trigger_texts,
        reminder_after=reminder_after,
        reminder_targets=reminder_targets,
        retention_days=retention_days,
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
        application_status_mode=application_status_mode,
        jev_api_key=jev_api_key,
        jev_model=jev_model,
        application_status_confidence_threshold=application_status_confidence_threshold,
    )
