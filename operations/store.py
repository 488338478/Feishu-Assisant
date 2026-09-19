"""Transactional SQLite store for operations configuration and run events."""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .configuration import load_seed_config, validate_config


MAX_RUN_TEXT_CHARS = 100_000
MAX_EVENT_BYTES = 512_000
MAX_FIELD_BYTES = 512_000
TERMINAL_STATUSES = {"completed", "failed", "partial", "cancelled", "interrupted"}
_PRIVATE_KEYS = {
    "chain_of_thought",
    "private_reasoning",
    "reasoning",
    "thinking",
    "thinking_block",
}
_SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "cookie",
    "credential",
    "credentials",
}
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([a-z0-9._~+/=-]{4,})")
_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|"
    r"client[_-]?secret|password|passwd|secret)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s,;&}\]\"']+)"
)


def _is_private_label(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.lower().replace("-", "_").replace(".", "_")
    return any(marker in normalized for marker in ("thinking", "reasoning", "chain_of_thought"))


class RevisionConflict(RuntimeError):
    """Raised when a write was based on an obsolete published revision."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub(r"\1[REDACTED]", value)
    return _ASSIGNMENT_RE.sub(r"\1[REDACTED]", value)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 32:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        block_type = value.get("type")
        if _is_private_label(block_type):
            return {"type": str(block_type), "omitted": True}
        result = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower().replace("-", "_")
            if normalized in _PRIVATE_KEYS:
                continue
            if normalized in _SECRET_KEYS or normalized.endswith("_token") or normalized.endswith("_secret"):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _redact(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


class OpsStore:
    """SQLite-backed revision, execution, and event store.

    A new connection is used per operation so the object is safe to share
    across worker threads. Writes use ``BEGIN IMMEDIATE`` to serialize revision
    publication and per-run event sequence allocation.
    """

    def __init__(self, path: str | os.PathLike[str], seed_config: Any = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(seed_config)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self, seed: Any) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS config_versions (
                    revision INTEGER PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    source_revision INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS config_drafts (
                    id TEXT PRIMARY KEY,
                    base_revision INTEGER NOT NULL,
                    config_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    published_revision INTEGER,
                    published_at TEXT
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    fields_json TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(revision) REFERENCES config_versions(revision)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS runs_source_message
                    ON runs(source, message_id) WHERE message_id <> '';
                CREATE INDEX IF NOT EXISTS runs_status_updated
                    ON runs(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE,
                    UNIQUE(run_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS run_events_cursor
                    ON run_events(run_id, id);
                """
            )
        finally:
            connection.close()

        with self._write() as connection:
            exists = connection.execute("SELECT 1 FROM config_versions LIMIT 1").fetchone()
            if exists is None:
                initial = load_seed_config() if seed is None else validate_config(seed)
                connection.execute(
                    """INSERT INTO config_versions
                       (revision, config_json, actor, action, source_revision, created_at)
                       VALUES (1, ?, ?, ?, NULL, ?)""",
                    (_json_dump(initial), "system", "seed", _now()),
                )

    @staticmethod
    def _version_from_row(row: sqlite3.Row, *, include_config: bool = True) -> dict[str, Any]:
        result = {
            "revision": row["revision"],
            "actor": row["actor"],
            "action": row["action"],
            "source_revision": row["source_revision"],
            "created_at": row["created_at"],
        }
        if include_config:
            result["config"] = json.loads(row["config_json"])
        return result

    @staticmethod
    def _current_row(connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM config_versions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:  # Protected by initialization; retained for corrupt DBs.
            raise RuntimeError("operations store has no published configuration")
        return row

    def snapshot(self) -> dict[str, Any]:
        with self._read() as connection:
            row = self._current_row(connection)
            return {
                "revision": row["revision"],
                "config": json.loads(row["config_json"]),
            }

    def save_draft(self, config: Any, base_revision: int, actor: str) -> dict[str, Any]:
        checked = validate_config(config)
        actor = self._bounded_string(actor, "actor", 512)
        if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 1:
            raise ValueError("base_revision must be a positive integer")
        draft_id = uuid.uuid4().hex
        created_at = _now()
        with self._write() as connection:
            current = self._current_row(connection)["revision"]
            if current != base_revision:
                raise RevisionConflict(
                    f"draft base revision {base_revision} does not match current revision {current}"
                )
            connection.execute(
                """INSERT INTO config_drafts
                   (id, base_revision, config_json, actor, status, created_at)
                   VALUES (?, ?, ?, ?, 'draft', ?)""",
                (draft_id, base_revision, _json_dump(checked), actor, created_at),
            )
        return {
            "id": draft_id,
            "base_revision": base_revision,
            "config": copy.deepcopy(checked),
            "actor": actor,
            "status": "draft",
            "created_at": created_at,
        }

    def publish(self, draft_id: str, actor: str) -> dict[str, Any]:
        draft_id = self._bounded_string(draft_id, "draft_id", 128)
        actor = self._bounded_string(actor, "actor", 512)
        with self._write() as connection:
            draft = connection.execute(
                "SELECT * FROM config_drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            if draft is None:
                raise KeyError(f"unknown draft {draft_id!r}")
            if draft["status"] != "draft":
                raise RevisionConflict(f"draft {draft_id!r} has already been published")
            current = self._current_row(connection)["revision"]
            if current != draft["base_revision"]:
                raise RevisionConflict(
                    f"draft base revision {draft['base_revision']} does not match current revision {current}"
                )
            config = validate_config(json.loads(draft["config_json"]))
            revision = current + 1
            timestamp = _now()
            connection.execute(
                """INSERT INTO config_versions
                   (revision, config_json, actor, action, source_revision, created_at)
                   VALUES (?, ?, ?, 'publish', ?, ?)""",
                (revision, _json_dump(config), actor, draft["base_revision"], timestamp),
            )
            connection.execute(
                """UPDATE config_drafts
                   SET status = 'published', published_revision = ?, published_at = ?
                   WHERE id = ?""",
                (revision, timestamp, draft_id),
            )
        return {"revision": revision, "config": copy.deepcopy(config)}

    def rollback(self, revision: int, expected_revision: int, actor: str) -> dict[str, Any]:
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
               for value in (revision, expected_revision)):
            raise ValueError("revision values must be positive integers")
        actor = self._bounded_string(actor, "actor", 512)
        with self._write() as connection:
            current = self._current_row(connection)["revision"]
            if current != expected_revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, current revision is {current}"
                )
            target = connection.execute(
                "SELECT config_json FROM config_versions WHERE revision = ?", (revision,)
            ).fetchone()
            if target is None:
                raise KeyError(f"unknown revision {revision}")
            config = validate_config(json.loads(target["config_json"]))
            new_revision = current + 1
            connection.execute(
                """INSERT INTO config_versions
                   (revision, config_json, actor, action, source_revision, created_at)
                   VALUES (?, ?, ?, 'rollback', ?, ?)""",
                (new_revision, _json_dump(config), actor, revision, _now()),
            )
        return {"revision": new_revision, "config": copy.deepcopy(config)}

    def versions(self) -> list[dict[str, Any]]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM config_versions ORDER BY revision DESC"
            ).fetchall()
        return [self._version_from_row(row) for row in rows]

    @staticmethod
    def _bounded_string(value: Any, name: str, maximum: int, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        if not allow_empty and not value:
            raise ValueError(f"{name} must not be empty")
        if len(value) > maximum:
            raise ValueError(f"{name} must contain at most {maximum} characters")
        return value

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        run = {
            "id": row["id"],
            "chat_id": row["chat_id"],
            "sender": row["sender"],
            "text": row["text"],
            "source": row["source"],
            "message_id": row["message_id"],
            "revision": row["revision"],
            "status": row["status"],
            "cancel_requested": bool(row["cancel_requested"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        run.update(json.loads(row["fields_json"]))
        return run

    def create_run(
        self,
        chat_id: str,
        sender: str,
        text: str,
        source: str = "im",
        message_id: str = "",
        revision: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        chat_id = self._bounded_string(chat_id, "chat_id", 512)
        sender = self._bounded_string(sender, "sender", 512, allow_empty=True)
        source = self._bounded_string(source, "source", 64)
        message_id = self._bounded_string(message_id, "message_id", 512, allow_empty=True)
        text = self._bounded_string(text, "text", MAX_RUN_TEXT_CHARS, allow_empty=True)
        text = _redact_text(text)
        run_id = uuid.uuid4().hex
        timestamp = _now()
        with self._write() as connection:
            if message_id:
                existing = connection.execute(
                    "SELECT * FROM runs WHERE source = ? AND message_id = ?",
                    (source, message_id),
                ).fetchone()
                if existing is not None:
                    return self._run_from_row(existing), False
            current_revision = self._current_row(connection)["revision"]
            selected_revision = current_revision if revision is None else revision
            if isinstance(selected_revision, bool) or not isinstance(selected_revision, int):
                raise ValueError("revision must be an integer")
            known = connection.execute(
                "SELECT 1 FROM config_versions WHERE revision = ?", (selected_revision,)
            ).fetchone()
            if known is None:
                raise KeyError(f"unknown revision {selected_revision}")
            try:
                connection.execute(
                    """INSERT INTO runs
                       (id, chat_id, sender, text, source, message_id, revision, status,
                        fields_json, cancel_requested, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', '{}', 0, ?, ?)""",
                    (
                        run_id, chat_id, sender, text, source, message_id,
                        selected_revision, timestamp, timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                if not message_id:
                    raise
                existing = connection.execute(
                    "SELECT * FROM runs WHERE source = ? AND message_id = ?",
                    (source, message_id),
                ).fetchone()
                if existing is None:
                    raise
                return self._run_from_row(existing), False
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return self._run_from_row(row), True

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "sequence": row["sequence"],
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def _emit_in_transaction(
        self, connection: sqlite3.Connection, run_id: str, kind: str, payload: Any
    ) -> dict[str, Any]:
        exists = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
        if exists is None:
            raise KeyError(f"unknown run {run_id!r}")
        clean_payload = {"omitted": True} if _is_private_label(kind) else _redact(payload)
        payload_json = _json_dump(clean_payload)
        if len(payload_json.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError(f"payload must be at most {MAX_EVENT_BYTES} bytes")
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        timestamp = _now()
        cursor = connection.execute(
            """INSERT INTO run_events (run_id, sequence, kind, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, sequence, kind, payload_json, timestamp),
        )
        return {
            "id": cursor.lastrowid,
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind,
            "payload": clean_payload,
            "created_at": timestamp,
        }

    def emit(self, run_id: str, kind: str, payload: Any) -> dict[str, Any]:
        run_id = self._bounded_string(run_id, "run_id", 128)
        kind = self._bounded_string(kind, "kind", 128)
        with self._write() as connection:
            return self._emit_in_transaction(connection, run_id, kind, payload)

    def update_run(self, run_id: str, status: str, **fields: Any) -> dict[str, Any]:
        run_id = self._bounded_string(run_id, "run_id", 128)
        status = self._bounded_string(status, "status", 64)
        protected = {
            "id", "chat_id", "sender", "text", "source", "message_id", "revision",
            "status", "created_at", "updated_at", "cancel_requested",
        }
        overlap = protected.intersection(fields)
        if overlap:
            raise ValueError(f"fields cannot replace protected values: {', '.join(sorted(overlap))}")
        clean_fields = _redact(fields)
        fields_json = _json_dump(clean_fields)
        if len(fields_json.encode("utf-8")) > MAX_FIELD_BYTES:
            raise ValueError(f"fields must be at most {MAX_FIELD_BYTES} bytes")
        with self._write() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown run {run_id!r}")
            merged = json.loads(row["fields_json"])
            merged.update(clean_fields)
            connection.execute(
                "UPDATE runs SET status = ?, fields_json = ?, updated_at = ? WHERE id = ?",
                (status, _json_dump(merged), _now(), run_id),
            )
            updated = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            return self._run_from_row(updated)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run_id = self._bounded_string(run_id, "run_id", 128)
        with self._read() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else self._run_from_row(row)

    def list_runs(self, status: str = "", query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        status = self._bounded_string(status, "status", 64, allow_empty=True)
        query = self._bounded_string(query, "query", 500, allow_empty=True)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        clauses: list[str] = []
        parameters: list[Any] = []
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        if query:
            clauses.append(
                "(chat_id LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\' "
                "OR text LIKE ? ESCAPE '\\' OR fields_json LIKE ? ESCAPE '\\')"
            )
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.extend([f"%{escaped}%"] * 4)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT * FROM runs{where} ORDER BY updated_at DESC, id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def events(self, run_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        run_id = self._bounded_string(run_id, "run_id", 128)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after must be a non-negative event id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self._read() as connection:
            rows = connection.execute(
                """SELECT * FROM run_events
                   WHERE run_id = ? AND id > ? ORDER BY id ASC LIMIT ?""",
                (run_id, after, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def cancel(self, run_id: str) -> bool:
        run_id = self._bounded_string(run_id, "run_id", 128)
        with self._write() as connection:
            row = connection.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return False
            if row["status"] in TERMINAL_STATUSES:
                return False
            status = "cancelled" if row["status"] == "queued" else row["status"]
            connection.execute(
                """UPDATE runs SET cancel_requested = 1, status = ?, updated_at = ?
                   WHERE id = ?""",
                (status, _now(), run_id),
            )
            self._emit_in_transaction(connection, run_id, "cancel_requested", {"status": status})
            return True

    def is_cancelled(self, run_id: str) -> bool:
        run_id = self._bounded_string(run_id, "run_id", 128)
        with self._read() as connection:
            row = connection.execute(
                "SELECT cancel_requested, status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))

    def recover_interrupted(self) -> int:
        """Mark queued/running work orphaned by process restart as interrupted."""

        with self._write() as connection:
            rows = connection.execute(
                "SELECT id, status FROM runs WHERE status IN ('queued', 'running')"
            ).fetchall()
            timestamp = _now()
            for row in rows:
                connection.execute(
                    "UPDATE runs SET status = 'interrupted', updated_at = ? WHERE id = ?",
                    (timestamp, row["id"]),
                )
                self._emit_in_transaction(
                    connection,
                    row["id"],
                    "interrupted",
                    {"reason": "process_restart", "previous_status": row["status"]},
                )
            return len(rows)


_default_store: OpsStore | None = None
_default_store_lock = threading.Lock()


def get_store() -> OpsStore:
    """Return the process-wide store, creating it only on first use."""

    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                package_dir = Path(__file__).resolve().parents[1]
                data_dir = Path(os.environ.get("ASSISTANT_DATA_DIR", package_dir / "data"))
                db_path = Path(os.environ.get("ASSISTANT_OPERATIONS_DB", data_dir / "operations.sqlite3"))
                _default_store = OpsStore(db_path)
    return _default_store


__all__ = ["OpsStore", "RevisionConflict", "get_store"]
