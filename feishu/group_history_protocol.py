"""Fail-closed protocol for agent-requested current-group history."""

from __future__ import annotations

import json
from dataclasses import dataclass


SEARCH_PREFIX = "[GROUP_HISTORY_SEARCH]"
EXPAND_PREFIX = "[GROUP_HISTORY_EXPAND]"


@dataclass(frozen=True)
class HistoryRequest:
    kind: str
    query: str = ""
    time_range_hours: int = 24
    sender_ids: tuple[str, ...] = ()
    thread_id: str = ""
    limit: int = 8
    message_id: str = ""
    part: int = 1


def _integer(value, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def parse_history_request(text: str) -> HistoryRequest | None:
    if not isinstance(text, str) or len(text) > 4096:
        return None
    value = text.strip()
    if value.startswith(SEARCH_PREFIX):
        kind, prefix = "search", SEARCH_PREFIX
        allowed = {"query", "time_range_hours", "sender_ids", "thread_id", "limit"}
    elif value.startswith(EXPAND_PREFIX):
        kind, prefix = "expand", EXPAND_PREFIX
        allowed = {"message_id", "part"}
    else:
        return None
    payload_text = value[len(prefix):]
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or set(payload) - allowed:
        return None

    if kind == "search":
        query = payload.get("query", "")
        hours = payload.get("time_range_hours", 24)
        sender_ids = payload.get("sender_ids", [])
        thread_id = payload.get("thread_id", "")
        limit = payload.get("limit", 8)
        if (
            not isinstance(query, str) or len(query) > 1000
            or not _integer(hours, 1, 24 * 365)
            or not isinstance(sender_ids, list)
            or len(sender_ids) > 50
            or not all(isinstance(item, str) and item for item in sender_ids)
            or not isinstance(thread_id, str)
            or not _integer(limit, 1, 50)
        ):
            return None
        return HistoryRequest(
            kind="search", query=query, time_range_hours=hours,
            sender_ids=tuple(sender_ids), thread_id=thread_id, limit=limit,
        )

    message_id = payload.get("message_id", "")
    part = payload.get("part", 1)
    if not isinstance(message_id, str) or not message_id or not _integer(part, 1, 10000):
        return None
    return HistoryRequest(kind="expand", message_id=message_id, part=part)
