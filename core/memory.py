"""L5: 跨会话语义记忆 — 自动学习 + 召回 + 持久化。"""
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime

from ..config import MEMORY_FILE, MEMORY_MAX_FACTS
from .vector_store import VectorStore

# L5: 跨会话记忆系统
# ============================================================================

LEGACY_SCOPE = "__legacy__"

class SemanticMemory:
    """持久化语义记忆，存储项目知识、人员信息、进展等事实。"""

    def __init__(self):
        self.facts: list[dict] = []        # [{id, content, source, tags, time}]
        self.store = VectorStore()
        self._lock = threading.RLock()
        self._load()

    @staticmethod
    def _scope(scope: str | None) -> str:
        return LEGACY_SCOPE if scope is None else str(scope)

    def _rebuild_store(self) -> None:
        store = VectorStore()
        for fact in self.facts:
            index_text = fact["content"] + " " + " ".join(fact.get("tags", []))
            store.add(
                fact["id"],
                index_text,
                {"source": fact["source"], "scope": fact["scope"]},
            )
        self.store = store

    def _truncate(self) -> None:
        if len(self.facts) > MEMORY_MAX_FACTS:
            self.facts = self.facts[:MEMORY_MAX_FACTS]
            self._rebuild_store()

    def _load(self) -> None:
        """从文件加载记忆。"""
        try:
            if MEMORY_FILE.exists():
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.facts = data.get("facts", [])[:MEMORY_MAX_FACTS]
                for fact in self.facts:
                    fact["scope"] = self._scope(fact.get("scope"))
                self._rebuild_store()
        except Exception as e:
            print(f"[MEMORY] load error: {e}", flush=True)

    def _save(self) -> None:
        """保存记忆到文件。"""
        try:
            with self._lock:
                self._truncate()
                data = {
                    "facts": self.facts,
                    "store": self.store.to_dict(),
                    "updated": int(datetime.now().timestamp()),
                }
                MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                temp_path = MEMORY_FILE.with_name(
                    f".{MEMORY_FILE.name}.{uuid.uuid4().hex}.tmp"
                )
                try:
                    with open(temp_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temp_path, MEMORY_FILE)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
        except Exception as e:
            print(f"[MEMORY] save error: {e}", flush=True)

    def remember(self, content: str, source: str = "conversation",
                 tags: list[str] = None, scope: str | None = None) -> str:
        """存入一条记忆。返回记忆 ID。"""
        fact_id = f"mem_{uuid.uuid4().hex}"
        normalized_scope = self._scope(scope)
        with self._lock:
            fact = {
                "id": fact_id,
                "content": content,
                "source": source,
                "tags": tags or [],
                "time": int(datetime.now().timestamp()),
                "scope": normalized_scope,
            }
            self.facts.insert(0, fact)
            index_text = content + " " + " ".join(tags or [])
            self.store.add(
                fact_id,
                index_text,
                {"source": source, "scope": normalized_scope},
            )
            self._save()
        print(f"[MEMORY] remembered: {content[:80]}...", flush=True)
        return fact_id

    def clear_scope(self, scope: str) -> int:
        """Remove all facts and index entries belonging to one conversation scope."""
        with self._lock:
            before = len(self.facts)
            self.facts = [f for f in self.facts if f.get("scope") != self._scope(scope)]
            if len(self.facts) != before:
                self._rebuild_store()
                self._save()
            return before - len(self.facts)

    def recall(self, query: str, top_k: int = 5,
               scope: str | None = None) -> list[dict]:
        """语义搜索记忆，返回相关事实。"""
        if top_k <= 0:
            return []
        normalized_scope = self._scope(scope)
        with self._lock:
            results = self.store.search(query, top_k=len(self.store.documents))
            facts_map = {fact["id"]: fact for fact in self.facts}
            enriched = []
            for result in results:
                fact = facts_map.get(result["id"])
                if fact and fact.get("scope") == normalized_scope:
                    enriched.append({
                        **result,
                        "content": fact["content"],
                        "source": fact["source"],
                        "tags": fact.get("tags", []),
                        "time": fact.get("time", 0),
                        "scope": fact["scope"],
                    })
                    if len(enriched) == top_k:
                        break
            return enriched

    def auto_learn(self, user_message: str, assistant_response: str,
                   scope: str | None = None) -> None:
        """从对话中自动提取关键信息并存入记忆。

        规则:
        - 包含人名+任务/状态 → 记住项目进展
        - 包含日期+事件 → 记住关键时间节点
        - 包含"进度"/"完成"/"开始" → 记住里程碑
        """
        # 检测进展信息
        progress_patterns = [
            (r'(\S+)(?:的|已经|正在|完成了?)(.+?)(?:了|。|，|$)', "progress"),
            (r'(?:今天|明天|本周|下周)(?:要|需要|计划)(.+?)(?:。|，|$)', "plan"),
            (r'(\S+)(?:负责|在做|处理)(.+?)(?:。|，|$)', "assignment"),
        ]
        for pattern, tag in progress_patterns:
            for m in re.finditer(pattern, user_message):
                self.remember(m.group(0).strip(),
                             source="auto_learn", tags=[tag, "auto"],
                             scope=scope)

    def format_context(self, query: str = "", top_k: int = 8,
                       scope: str | None = None) -> str:
        """格式化记忆上下文，注入 Claude prompt。"""
        if query:
            facts = self.recall(query, top_k=top_k, scope=scope)
        else:
            # 返回最近的记忆
            normalized_scope = self._scope(scope)
            facts = [
                {"content": f["content"], "source": f["source"],
                 "score": 1.0, "time": f.get("time", 0), "tags": f.get("tags", [])}
                for f in self.facts if f.get("scope") == normalized_scope
            ][:top_k]

        if not facts:
            return ""

        lines = ["\n## 记忆库（相关历史信息）"]
        for i, fact in enumerate(facts, 1):
            t = datetime.fromtimestamp(fact.get("time", 0)).strftime("%m-%d %H:%M")
            tags_str = f" [{', '.join(fact.get('tags', []))}]" if fact.get("tags") else ""
            lines.append(f"{i}. [{t}]{tags_str} {fact['content']}")
        return "\n".join(lines)

# 全局记忆实例
memory = SemanticMemory()
