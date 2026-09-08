"""Pure helpers for deciding whether an incoming message targets the bot."""

from collections.abc import Iterable
from typing import Any


def _mention_open_id(mention: Any) -> str:
    mention_id = getattr(mention, "id", None)
    return getattr(mention_id, "open_id", "") or ""


def message_targets_bot(
    chat_type: str,
    mentions: Iterable[Any] | None,
    bot_open_id: str,
) -> bool:
    """Allow all private messages and only explicit bot mentions in groups."""
    if chat_type == "p2p":
        return True
    if chat_type != "group" or not bot_open_id:
        return False
    return any(_mention_open_id(item) == bot_open_id for item in mentions or [])


def strip_bot_mention(
    text: str,
    mentions: Iterable[Any] | None,
    bot_open_id: str,
) -> str:
    """Remove only the placeholder corresponding to the current bot."""
    result = text
    for item in mentions or []:
        if _mention_open_id(item) != bot_open_id:
            continue
        key = getattr(item, "key", "") or ""
        if key:
            result = result.replace(key, "")
    return result.strip()
