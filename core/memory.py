"""L5: 跨会话语义记忆 — 自动学习 + 召回 + 持久化。"""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from ..config import MEMORY_FILE, MEMORY_MAX_FACTS
from .vector_store import VectorStore

# L5: 跨会话记忆系统
# ============================================================================

class SemanticMemory:
    """持久化语义记忆，存储项目知识、人员信息、进展等事实。"""

    def __init__(self):
        self.facts: list[dict] = []        # [{id, content, source, tags, time}]
        self.store = VectorStore()
        self._load()

    def _load(self) -> None:
        """从文件加载记忆。"""
        try:
            if MEMORY_FILE.exists():
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.facts = data.get("facts", [])
                if data.get("store"):
                    self.store = VectorStore.from_dict(data["store"])
        except Exception as e:
            print(f"[MEMORY] load error: {e}", flush=True)

    def _save(self) -> None:
        """保存记忆到文件。"""
        try:
            data = {
                "facts": self.facts[-MEMORY_MAX_FACTS:],  # 保留最近的
                "store": self.store.to_dict(),
                "updated": int(datetime.now().timestamp()),
            }
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[MEMORY] save error: {e}", flush=True)

    def remember(self, content: str, source: str = "conversation",
                 tags: list[str] = None) -> str:
        """存入一条记忆。返回记忆 ID。"""
        fact_id = f"mem_{int(time.time()*1000)}"
        fact = {
            "id": fact_id,
            "content": content,
            "source": source,
            "tags": tags or [],
            "time": int(datetime.now().timestamp()),
        }
        self.facts.insert(0, fact)
        # 加入向量索引
        index_text = content + " " + " ".join(tags or [])
        self.store.add(fact_id, index_text, {"source": source})
        self._save()
        print(f"[MEMORY] remembered: {content[:80]}...", flush=True)
        return fact_id

    def recall(self, query: str, top_k: int = 5) -> list[dict]:
        """语义搜索记忆，返回相关事实。"""
        results = self.store.search(query, top_k=top_k)
        # 关联完整事实数据
        facts_map = {f["id"]: f for f in self.facts}
        enriched = []
        for r in results:
            fact = facts_map.get(r["id"])
            if fact:
                enriched.append({
                    **r,
                    "content": fact["content"],
                    "source": fact["source"],
                    "tags": fact.get("tags", []),
                    "time": fact.get("time", 0),
                })
        return enriched

    def auto_learn(self, user_message: str, assistant_response: str) -> None:
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
                             source="auto_learn", tags=[tag, "auto"])

    def format_context(self, query: str = "", top_k: int = 8) -> str:
        """格式化记忆上下文，注入 Claude prompt。"""
        if query:
            facts = self.recall(query, top_k=top_k)
        else:
            # 返回最近的记忆
            facts = [
                {"content": f["content"], "source": f["source"],
                 "score": 1.0, "time": f.get("time", 0), "tags": f.get("tags", [])}
                for f in self.facts[:top_k]
            ]

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
