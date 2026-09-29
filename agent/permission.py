"""Small, deterministic helpers for Feishu-mediated Claude permissions."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping


_DECISIONS = {
    "允许一次": "once",
    "允许本次请求": "once",
    "允许本次会话": "session",
    "拒绝": "deny",
    "拒绝本次请求": "deny",
}


def decision_from_text(text: str) -> str | None:
    """Parse only exact approval commands; natural-language guesses are ignored."""
    return _DECISIONS.get("".join(str(text).lower().split()))


def is_admin(sender_id: str, admin_ids: Iterable[str]) -> bool:
    return bool(sender_id) and sender_id in {item for item in admin_ids if item}


def session_allow_pattern(tool_name: str, tool_input: Mapping[str, object]) -> str:
    """Return the narrowest practical Claude allow pattern for a repeated tool."""
    if tool_name != "Bash":
        return tool_name
    command = tool_input.get("command")
    if not isinstance(command, str):
        return "Bash"
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return "Bash"
    if not tokens:
        return "Bash"
    # Keep the executable/subcommand and a short option; values remain wildcarded.
    prefix_tokens = tokens[:3] if len(tokens) >= 3 and tokens[2].startswith("-") else tokens[:2]
    prefix = " ".join(prefix_tokens)
    if not all(re.fullmatch(r"[A-Za-z0-9_./:+=-]+", token) for token in prefix_tokens):
        return "Bash"
    return f"Bash({prefix}:*)"
