import discord

MAX_FIELD_VALUE = 1024
MAX_DESCRIPTION = 4096

COLOR_INFO = discord.Color.blurple()
COLOR_SUCCESS = discord.Color.green()
COLOR_REMINDER = discord.Color.orange()
COLOR_ERROR = discord.Color.red()
COLOR_CANCELLED = discord.Color.dark_gray()


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def error_embed(description: str) -> discord.Embed:
    return discord.Embed(
        title="エラー",
        description=truncate(description, MAX_DESCRIPTION),
        color=COLOR_ERROR,
    )


def success_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=truncate(description, MAX_DESCRIPTION),
        color=COLOR_SUCCESS,
    )
