"""L2: 主动上下文组装 — 搜索 KB + 记忆召回 + 链接提取 + 关键词检测。"""
import json
import re
from datetime import datetime

from ..config import THESIS_DIR, WIKI_INDEX_FILE, WIKI_INDEX_TTL
from ..feishu.client import (
    fetch_wiki_node, fetch_doc_raw, fetch_bitable,
    get_calendar_agenda, get_my_tasks, search_meetings,
)
from ..core.memory import memory
from ..core.vector_store import VectorStore
from .. import thesis as thesis_mod

import time

# Wiki 索引缓存
_wiki_index_cache = None

def _load_wiki_index_from_file():
    """从缓存文件加载 wiki 索引。"""
    global _wiki_index_cache
    try:
        if WIKI_INDEX_FILE.exists():
            import json as _json
            with open(WIKI_INDEX_FILE, "r", encoding="utf-8") as f:
                _wiki_index_cache = _json.load(f)
    except Exception:
        pass
    return _wiki_index_cache

def get_wiki_index(force_refresh: bool = False) -> dict:
    """获取 wiki 索引，过期则返回 None（需要重建）。"""
    if not force_refresh and _wiki_index_cache:
        age = int(time.time()) - _wiki_index_cache.get("last_updated", 0)
        if age < WIKI_INDEX_TTL:
            return _wiki_index_cache
    return _load_wiki_index_from_file() or {}

def format_wiki_index_summary(index: dict) -> str:
    """格式化知识库概览。"""
    spaces = index.get("spaces", {})
    if not spaces: return ""
    lines = ["## 知识库概览"]
    for sid, sdata in spaces.items():
        name = sdata.get("name", sid)
        count = sdata.get("node_count", 0)
        lines.append(f"- **{name}** ({count} 个节点)")
        top = [n for n in sdata.get("nodes", {}).values() if n.get("parent_token") is None][:10]
        for node in top:
            lines.append(f"  - {node['title']} `{node.get('obj_type','')}`")
    return "\n".join(lines)

def extract_feishu_urls(text: str, client) -> list[str]:
    """从消息中提取飞书链接并获取内容。"""
    extras = []
    for m in re.finditer(r'feishu\.cn/wiki/([A-Za-z0-9]+)', text):
        extras.append(f"[飞书 Wiki 内容]\n{fetch_wiki_node(client, m.group(1))}")
    for m in re.finditer(r'feishu\.cn/docx/([A-Za-z0-9]+)', text):
        extras.append(f"[飞书文档内容]\n{fetch_doc_raw(client, m.group(1))}")
    for m in re.finditer(r'feishu\.cn/base/([A-Za-z0-9]+)[^"]*[?&]table=([A-Za-z0-9]+)', text):
        extras.append(f"[多维表格内容]\n{fetch_bitable(client, m.group(1), m.group(2))}")
    return extras

def build_proactive_context(text: str, client) -> str:
    """主动搜索并组装上下文：记忆 → 知识库 → 关键词检测。"""
    context_parts = []

    # 0. 语义记忆搜索
    try:
        if len(text) > 3:
            mem_ctx = memory.format_context(query=text, top_k=5)
            if mem_ctx: context_parts.append(mem_ctx)
    except Exception as e:
        print(f"[CONTEXT] memory: {e}", flush=True)

    # 1. 知识库概览
    try:
        index = get_wiki_index()
        if index.get("spaces"):
            context_parts.append(format_wiki_index_summary(index))
    except Exception as e:
        print(f"[CONTEXT] wiki: {e}", flush=True)

    # 2. 飞书链接
    try:
        urls = extract_feishu_urls(text, client)
        if urls: context_parts.extend(urls)
    except Exception as e:
        print(f"[CONTEXT] urls: {e}", flush=True)

    # 3. 毕设关键词
    if any(kw in text for kw in ["毕设", "毕业设计", "卦阵手记", "论文", "战斗系统",
                                   "战斗循环", "雁门关", "一页纸策划", "裴长宁", "方位战斗", "阵型"]):
        try:
            context_parts.append(thesis_mod.get_thesis_overview())
            for f in thesis_mod.list_thesis_files():
                if f.suffix == ".md" and any(kw in text for kw in [f.stem, "全部", "所有"]):
                    content = thesis_mod.read_thesis_file(f.name)
                    context_parts.append(f"\n## 毕设: {f.name}\n{content[:3000]}")
        except Exception as e:
            print(f"[CONTEXT] thesis: {e}", flush=True)

    # 4. 会议/日程/任务关键词
    if any(kw in text for kw in ["会议", "日程", "今天有什么", "安排", "agenda"]):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            agenda = get_calendar_agenda(today, today)
            if agenda: context_parts.append(f"\n## 今日日程\n{json.dumps(agenda, ensure_ascii=False)[:2000]}")
        except: pass

    if any(kw in text for kw in ["任务", "待办", "todo", "task", "进展", "进度", "做到什么地步"]):
        try:
            tasks = get_my_tasks()
            if tasks: context_parts.append(f"\n## 当前任务\n{json.dumps(tasks, ensure_ascii=False)[:2000]}")
        except: pass

    if any(kw in text for kw in ["会议纪要", "meeting", "minutes"]):
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            meetings = search_meetings(today, today)
            if meetings: context_parts.append(f"\n## 今日会议\n{json.dumps(meetings, ensure_ascii=False)[:2000]}")
        except: pass

    return "\n\n".join(context_parts) if context_parts else ""
