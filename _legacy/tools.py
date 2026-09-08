"""L3: 工具执行器 — 11 个工具的参数解析与执行。"""
import json
import re
from datetime import datetime

from .client import (
    search_docs, fetch_doc_via_cli, update_document, create_document,
    search_meetings, get_meeting_minute_token, get_meeting_notes,
    get_calendar_agenda, get_my_tasks, search_tasks,
)

TOOL_PATTERN = re.compile(r'\[TOOL:(\w+)\]\n?(.*?)\n?\[/TOOL\]', re.DOTALL)

def parse_tool_params(param_text: str) -> dict:
    """解析工具参数：每行 key=value 格式。

    content 参数会吞掉其后的所有行（支持多行正文），因此约定放在最后一个参数。
    """
    params = {}
    lines = param_text.strip().split("\n")
    for i, raw in enumerate(lines):
        line = raw.strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key == "content":
            params[key] = "\n".join([value] + lines[i + 1:]).strip()
            return params
        params[key] = value.strip()
    if not params and param_text.strip():
        single = param_text.strip()
        if "\n" not in single:
            params["query"] = single
    return params

def execute_tool(name: str, params: dict, client) -> str:
    """执行单个工具调用。需要从外部注入 memory 和 thesis 模块。"""
    print(f"[TOOL] execute: {name} params={params}", flush=True)

    try:
        if name == "search_kb":
            query = params.get("query", params.get("q", ""))
            if not query: return "[工具错误] search_kb 需要 query 参数"
            results = search_docs(query)
            if not results: return f"未找到与「{query}」相关的文档。"
            lines = [f"搜索「{query}」的结果："]
            for i, r in enumerate(results[:8], 1):
                title = r.get("title", r.get("name", "未知"))
                snippet = str(r.get("snippet", r.get("content", "")))[:300]
                token = r.get("obj_token", r.get("token", r.get("document_id", "")))
                url = r.get("url", "")
                lines.append(f"{i}. **{title}**\ntoken={token}")
                if snippet: lines.append(f"摘要: {snippet}")
                if url: lines.append(f"链接: {url}")
            return "\n".join(lines)

        elif name == "fetch_doc":
            token = params.get("token", params.get("doc_token", ""))
            if not token: return "[工具错误] fetch_doc 需要 token 参数"
            content = fetch_doc_via_cli(token)
            if len(content) > 5000: content = content[:5000] + "\n\n...(内容过长)"
            return f"文档内容 (token={token}):\n\n{content}"

        elif name == "read_thesis":
            from .. import thesis as thesis_mod
            file_param = params.get("file", params.get("name", ""))
            if not file_param: return "[工具错误] read_thesis 需要 file 参数"
            if file_param == "list": return thesis_mod.get_thesis_overview()
            content = thesis_mod.read_thesis_file(file_param)
            if len(content) > 5000: content = content[:5000] + "\n\n...(内容过长)"
            return f"毕设文件 {file_param}:\n\n{content}"

        elif name == "write_thesis":
            from .. import thesis as thesis_mod
            file_param = params.get("file", params.get("name", ""))
            if not file_param: return "[工具错误] write_thesis 需要 file 参数"
            content = params.get("content", "")
            if not content: return "[工具错误] write_thesis 需要 content 参数"
            return thesis_mod.write_thesis_file(file_param, content)

        elif name == "search_meetings":
            start = params.get("start", datetime.now().strftime("%Y-%m-%d"))
            end = params.get("end", datetime.now().strftime("%Y-%m-%d"))
            meetings = search_meetings(start, end)
            return json.dumps(meetings, ensure_ascii=False, indent=2)[:3000] if meetings else f"{start} 至 {end} 期间没有会议。"

        elif name == "get_meeting_notes":
            meeting_id = params.get("meeting_id", "")
            if not meeting_id: return "[工具错误] get_meeting_notes 需要 meeting_id 参数"
            minute_token = get_meeting_minute_token(meeting_id)
            if not minute_token: return f"会议 {meeting_id} 未找到纪要。"
            notes = get_meeting_notes(minute_token)
            return notes[:5000] if len(notes) > 5000 else notes

        elif name == "get_calendar":
            date_str = params.get("date", datetime.now().strftime("%Y-%m-%d"))
            agenda = get_calendar_agenda(date_str, date_str)
            return json.dumps(agenda, ensure_ascii=False, indent=2)[:3000] if agenda else f"{date_str} 没有日程。"

        elif name == "get_tasks":
            query = params.get("query", "")
            tasks = get_my_tasks() if not query else search_tasks(query)
            return json.dumps(tasks, ensure_ascii=False, indent=2)[:3000] if tasks else "没有找到任务。"

        elif name == "create_doc":
            title = params.get("title", "未命名文档")
            content = params.get("content", "")
            wiki_space = params.get("wiki_space", None)
            return json.dumps(create_document(title, content, wiki_space), ensure_ascii=False, indent=2)

        elif name == "remember":
            from ..core.memory import memory
            content = params.get("content", "")
            if not content: return "[工具错误] remember 需要 content 参数"
            tags = [t.strip() for t in params.get("tags", "").split(",") if t.strip()]
            memory.remember(content, source="claude_tool", tags=tags)
            return f"已记住：{content[:100]}"

        elif name == "recall":
            from ..core.memory import memory
            query = params.get("query", "")
            if not query: return "[工具错误] recall 需要 query 参数"
            facts = memory.recall(query, top_k=5)
            if not facts: return f"未找到与「{query}」相关的记忆。"
            lines = [f"记忆搜索「{query}」结果："]
            for i, f in enumerate(facts, 1):
                t = datetime.fromtimestamp(f.get("time", 0)).strftime("%m-%d %H:%M")
                lines.append(f"{i}. [{t}] {f['content']} (相似度:{f['score']:.2f})")
            return "\n".join(lines)

        else:
            return f"[工具错误] 未知工具: {name}"

    except Exception as e:
        print(f"[TOOL] error: {e}", flush=True)
        return f"[工具执行错误] {name}: {e}"

def extract_tools_and_clean(response: str) -> tuple:
    """从响应中提取工具调用并移除。"""
    tools = []
    for m in TOOL_PATTERN.finditer(response):
        name = m.group(1)
        params = parse_tool_params(m.group(2))
        tools.append((name, params))
    clean = TOOL_PATTERN.sub("", response).strip()
    return tools, clean
