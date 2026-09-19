"""评论投递日志：推理与回复分开持久化，重投不能重复修改文档。"""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time


class CommentStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS comments (key TEXT PRIMARY KEY, "
                       "state TEXT NOT NULL, response TEXT, updated REAL NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(comments)")}
            if "delivery_baseline" not in columns:
                db.execute("ALTER TABLE comments ADD COLUMN delivery_baseline TEXT")
            # 读取中断没有执行副作用，可以在事件重投时重试；running 不自动重置。
            db.execute("UPDATE comments SET state='received' WHERE state='reading'")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, key: str) -> tuple[str, str | None]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,response FROM comments WHERE key=?", (key,)).fetchone()
            if row and row[0] not in {"received"}:
                return row
            db.execute("INSERT INTO comments (key,state,response,updated) VALUES (?, 'reading', NULL, ?) "
                       "ON CONFLICT(key) DO UPDATE SET state='reading',updated=excluded.updated",
                       (key, time.time()))
            return "new", None

    def delivery_baseline(self, key: str, reply_ids: list[str]) -> set[str] | None:
        """首次发送前保存已存在的回复；重试时只核对之后新增的 bot 回复。"""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT delivery_baseline FROM comments WHERE key=?", (key,)).fetchone()
            if row and row[0] is not None:
                return set(json.loads(row[0]))
            db.execute("UPDATE comments SET delivery_baseline=? WHERE key=?",
                       (json.dumps(reply_ids), key))
            return None

    def set_state(self, key: str, state: str, response: str | None = None) -> None:
        with self.connection() as db:
            db.execute("UPDATE comments SET state=?,response=?,updated=? WHERE key=?",
                       (state, response, time.time(), key))
            # 仅清理已结束记录；结果待发和执行结果不确定的记录必须保留。
            db.execute("DELETE FROM comments WHERE state='done' AND updated<?",
                       (time.time() - 7 * 86400,))
