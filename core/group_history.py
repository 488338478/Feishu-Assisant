"""Current-group message storage and retrieval primitives."""

from __future__ import annotations

import sqlite3
import threading
import json
import math
import re
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from ..config import GROUP_HISTORY_CONFIG_FILE, GROUP_HISTORY_DB_FILE


@dataclass(frozen=True)
class GroupHistoryConfig:
    enabled: bool = True
    natural_soft_chars: int = 3000
    structured_soft_chars: int = 2000
    hard_chars: int = 12000
    retrieval_token_budget: int = 6000
    default_limit: int = 8
    max_search_rounds: int = 2
    sender_names: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.sender_names is None:
            object.__setattr__(self, "sender_names", {})


class GroupHistoryConfigLoader:
    def __init__(self, path: Path = GROUP_HISTORY_CONFIG_FILE) -> None:
        self.path = Path(path)
        self._last_valid = GroupHistoryConfig()
        self._lock = threading.RLock()

    @staticmethod
    def _validate(payload: dict) -> GroupHistoryConfig:
        defaults = GroupHistoryConfig()
        known = set(defaults.__dataclass_fields__)
        if set(payload) - known:
            raise ValueError("unknown configuration keys")
        values = {name: getattr(defaults, name) for name in known}
        values.update(payload)
        integer_names = {
            "natural_soft_chars", "structured_soft_chars", "hard_chars",
            "retrieval_token_budget", "default_limit", "max_search_rounds",
        }
        if not isinstance(values["enabled"], bool):
            raise ValueError("enabled must be boolean")
        if any(type(values[name]) is not int or values[name] <= 0 for name in integer_names):
            raise ValueError("limits must be positive integers")
        if values["default_limit"] > 50 or values["max_search_rounds"] > 10:
            raise ValueError("limits exceed safe maximum")
        if values["hard_chars"] < max(values["natural_soft_chars"], values["structured_soft_chars"]):
            raise ValueError("hard threshold must be largest")
        if values["retrieval_token_budget"] < max(
            values["natural_soft_chars"], values["structured_soft_chars"]
        ):
            raise ValueError("retrieval budget must fit one source slice")
        names = values["sender_names"]
        if not isinstance(names, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in names.items()
        ):
            raise ValueError("sender_names must map strings to strings")
        return GroupHistoryConfig(**values)

    def load(self) -> GroupHistoryConfig:
        with self._lock:
            try:
                if not self.path.exists():
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self.path.write_text(
                        json.dumps(self._last_valid.__dict__, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    return self._last_valid
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("configuration root must be an object")
                self._last_valid = self._validate(payload)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                print(f"[GROUP_HISTORY] config error: {error}", flush=True)
            return self._last_valid


@dataclass(frozen=True)
class GroupMessage:
    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    sent_at_ms: int
    reply_to: str
    thread_id: str
    content_type: str
    content: str


@dataclass(frozen=True)
class HistoryQuery:
    query: str = ""
    time_range_hours: int = 24
    sender_ids: Sequence[str] = ()
    thread_id: str = ""
    limit: int = 8
    exclude_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoryResult:
    text: str
    message_ids: tuple[str, ...]
    estimated_tokens: int
    truncated: bool


class MessageNotFound(LookupError):
    pass


class GroupHistoryStore:
    def __init__(self, path: Path = GROUP_HISTORY_DB_FILE) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS group_messages (
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    sent_at_ms INTEGER NOT NULL,
                    reply_to TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    PRIMARY KEY (chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_group_messages_time
                    ON group_messages(chat_id, sent_at_ms DESC);
                CREATE INDEX IF NOT EXISTS idx_group_messages_thread
                    ON group_messages(chat_id, thread_id, sent_at_ms);
                """
            )

    @staticmethod
    def _message(row: sqlite3.Row) -> GroupMessage:
        return GroupMessage(**dict(row))

    def record(self, message: GroupMessage) -> None:
        with self._lock, closing(self._connect()) as connection:
            with connection:
                connection.execute(
                """
                INSERT INTO group_messages
                    (chat_id, message_id, sender_id, sender_name, sent_at_ms,
                     reply_to, thread_id, content_type, content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, message_id) DO UPDATE SET
                    sender_id=excluded.sender_id,
                    sender_name=excluded.sender_name,
                    sent_at_ms=excluded.sent_at_ms,
                    reply_to=excluded.reply_to,
                    thread_id=excluded.thread_id,
                    content_type=excluded.content_type,
                    content=excluded.content
                """,
                (
                    message.chat_id, message.message_id, message.sender_id,
                    message.sender_name, message.sent_at_ms, message.reply_to,
                    message.thread_id, message.content_type, message.content,
                ),
            )

    def remove(self, chat_id: str, message_id: str) -> bool:
        with self._lock, closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM group_messages WHERE chat_id = ? AND message_id = ?",
                    (chat_id, message_id),
                )
                return cursor.rowcount > 0

    def get(self, chat_id: str, message_id: str) -> GroupMessage | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM group_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
        return self._message(row) if row else None

    def recent(
        self,
        chat_id: str,
        *,
        since_ms: int = 0,
        sender_ids: Sequence[str] = (),
        thread_id: str = "",
        limit: int = 200,
    ) -> list[GroupMessage]:
        clauses = ["chat_id = ?", "sent_at_ms >= ?"]
        values: list[object] = [chat_id, since_ms]
        if sender_ids:
            clauses.append(f"sender_id IN ({','.join('?' for _ in sender_ids)})")
            values.extend(sender_ids)
        if thread_id:
            clauses.append("(thread_id = ? OR message_id = ? OR reply_to = ?)")
            values.extend((thread_id, thread_id, thread_id))
        values.append(max(1, min(int(limit), 500)))
        query = (
            "SELECT * FROM group_messages WHERE " + " AND ".join(clauses)
            + " ORDER BY sent_at_ms DESC LIMIT ?"
        )
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._message(row) for row in rows]


class GroupHistoryService:
    _DISPLAY_TIMEZONE = timezone(timedelta(hours=8))

    def __init__(self, store: GroupHistoryStore, config_loader: GroupHistoryConfigLoader) -> None:
        self.store = store
        self.config_loader = config_loader

    @staticmethod
    def _score(message: GroupMessage, query: str) -> tuple[int, int]:
        normalized = query.strip().lower()
        if not normalized:
            return 0, message.sent_at_ms
        content = message.content.lower()
        exact = 1 if normalized in content else 0
        terms = [term for term in normalized.split() if term]
        occurrences = sum(content.count(term) for term in terms)
        return exact * 1000 + occurrences, message.sent_at_ms

    @classmethod
    def _prefix(cls, message: GroupMessage, suffix: str = "") -> str:
        stamp = datetime.fromtimestamp(
            message.sent_at_ms / 1000, cls._DISPLAY_TIMEZONE
        ).strftime("%H:%M")
        return f"[{stamp} {message.sender_name or message.sender_id}{suffix}] "

    @staticmethod
    def _is_structured(message: GroupMessage) -> bool:
        if message.content_type not in {"text", "post"}:
            return True
        content = message.content
        stripped = content.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                json.loads(stripped)
                return True
            except json.JSONDecodeError:
                pass
        lines = content.splitlines()
        if len(lines) < 8:
            return False
        log_lines = sum(
            bool(re.search(r"(?:^|\s)(DEBUG|INFO|WARN|ERROR|TRACE|FATAL)(?:\s|:)", line))
            for line in lines
        )
        stack_lines = sum(
            line.lstrip().startswith(("at ", "File \"", "Traceback", "Caused by:"))
            for line in lines
        )
        repeated = len(lines) - len(set(lines))
        return (
            log_lines / len(lines) >= 0.25
            or stack_lines / len(lines) >= 0.15
            or repeated / len(lines) >= 0.4
        )

    @classmethod
    def _whole(cls, message: GroupMessage) -> str:
        return cls._prefix(message) + message.content

    @classmethod
    def _part(cls, message: GroupMessage, part: int, size: int) -> str:
        total = max(1, math.ceil(len(message.content) / size))
        if part < 1 or part > total:
            raise MessageNotFound("message part unavailable")
        content = message.content[(part - 1) * size:part * size]
        suffix = (
            f"｜原文片段 {part}/{total}｜可继续展开"
            f"｜message_id={message.message_id}"
        )
        return cls._prefix(message, suffix) + content

    @classmethod
    def _part_size(cls, message: GroupMessage, config: GroupHistoryConfig) -> int:
        return (
            config.structured_soft_chars
            if cls._is_structured(message)
            else config.natural_soft_chars
        )

    def search(
        self, chat_id: str, query: HistoryQuery, now_ms: int | None = None
    ) -> HistoryResult:
        config = self.config_loader.load()
        if not config.enabled:
            return HistoryResult("[检索结果] 当前群聊天记录检索已关闭。", (), 0, False)
        current_ms = now_ms if now_ms is not None else int(datetime.now().timestamp() * 1000)
        since_ms = current_ms - max(1, query.time_range_hours) * 60 * 60 * 1000
        messages = self.store.recent(
            chat_id,
            since_ms=since_ms,
            sender_ids=query.sender_ids,
            thread_id=query.thread_id,
            limit=200,
        )
        excluded = set(query.exclude_message_ids)
        messages = [message for message in messages if message.message_id not in excluded]
        ranked = sorted(
            messages, key=lambda message: self._score(message, query.query), reverse=True
        )[:max(1, min(query.limit or config.default_limit, 50))]
        entries: list[tuple[GroupMessage, str]] = []
        used = 0
        truncated = False
        for message in ranked:
            whole = self._whole(message)
            structured = self._is_structured(message)
            soft_limit = self._part_size(message, config)
            must_slice = len(message.content) > config.hard_chars
            prefer_slice = structured and len(message.content) > soft_limit
            fits = used + len(whole) <= config.retrieval_token_budget
            if not must_slice and not prefer_slice and fits:
                item = whole
            elif not structured and not must_slice and fits:
                item = whole
            else:
                item = self._part(message, 1, soft_limit)
                truncated = True
            if entries and used + len(item) > config.retrieval_token_budget:
                continue
            entries.append((message, item))
            used += len(item)
        entries.sort(key=lambda entry: entry[0].sent_at_ms)
        text = "\n".join(item for _, item in entries)
        return HistoryResult(
            text or "[检索结果] 当前群没有命中消息。",
            tuple(message.message_id for message, _ in entries),
            len(text),
            truncated,
        )

    def expand(self, chat_id: str, message_id: str, part: int) -> HistoryResult:
        config = self.config_loader.load()
        message = self.store.get(chat_id, message_id)
        if message is None:
            raise MessageNotFound("message unavailable in current group")
        text = self._part(message, part, self._part_size(message, config))
        return HistoryResult(text, (message.message_id,), len(text), True)


group_history_config = GroupHistoryConfigLoader()
group_history_store = GroupHistoryStore()
group_history_service = GroupHistoryService(group_history_store, group_history_config)
