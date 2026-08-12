"""Channel interfaces with optional implementations loaded lazily."""

from mybot.channel.base import Channel

__all__ = ["Channel", "TelegramChannel", "DiscordChannel"]


def __getattr__(name: str):
    if name == "TelegramChannel":
        from mybot.channel.telegram_channel import TelegramChannel

        return TelegramChannel
    if name == "DiscordChannel":
        from mybot.channel.discord_channel import DiscordChannel

        return DiscordChannel
    raise AttributeError(name)
