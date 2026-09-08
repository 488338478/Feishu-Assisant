"""飞书 SDK + lark-cli 封装 — harness 侧仅保留 IM 发送 / reaction / 评论回复 / scheduler 所需函数。

文档/表格/会议等读写能力已全部移交 agent runtime（claude CLI + lark-cli skill），
不再在 Python 侧包装。
"""
import json
import re
import subprocess
import threading

import lark_oapi as lark
from lark_oapi.api.im.v1 import *

from ..config import APP_ID, APP_SECRET, LARK_CLI_PROFILE, LARK_CLI_TIMEOUT

# ═══════════════════════════════════════════════════════════════════════
# lark-cli subprocess（scheduler 与评论回复使用）
# ═══════════════════════════════════════════════════════════════════════

def run_lark_cli(args: list, timeout: int = LARK_CLI_TIMEOUT, as_bot: bool = True) -> dict:
    """运行 lark-cli 命令并返回解析后的 JSON。"""
    cmd = ["lark-cli", "--profile", LARK_CLI_PROFILE]
    if as_bot:
        cmd += ["--as", "bot"]
    cmd += args + ["--format", "json"]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0 and not stdout:
            return {"ok": False, "error": stderr[:500] or f"exit code {result.returncode}"}

        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("{") or line.startswith("["):
                return json.loads(line)
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"lark-cli 超时 ({timeout}s)"}
    except json.JSONDecodeError:
        return {"ok": False, "error": f"无法解析 lark-cli 输出: {stdout[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ═══════════════════════════════════════════════════════════════════════
# 飞书 SDK（线程安全单例）
# ═══════════════════════════════════════════════════════════════════════

_client = None
_client_lock = threading.Lock()

def build_client():
    """返回缓存的飞书 SDK client（避免每次调用重建）。"""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).build()
    return _client

# ═══════════════════════════════════════════════════════════════════════
# 消息发送
# ═══════════════════════════════════════════════════════════════════════

def send_message_text(client, chat_id: str, text: str) -> None:
    content = json.dumps({"text": text}, ensure_ascii=False)
    req = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(CreateMessageRequestBody.builder()
            .receive_id(chat_id).msg_type("text").content(content).build()).build()
    client.im.v1.message.create(req)

def _build_post_content(text: str) -> dict:
    """Markdown → 飞书 post 格式（简化版）。"""
    paragraphs = text.strip().split("\n\n")
    content = {"zh_cn": {"title": "", "content": []}}
    for para in paragraphs:
        para = para.strip()
        if not para: continue
        lines = para.split("\n")
        para_elements = []
        for i, line in enumerate(lines):
            heading = re.match(r'^(#{1,3})\s+(.+)$', line)
            list_item = re.match(r'^[-*]\s+(.+)$', line)
            if heading:
                para_elements.append([{"tag": "b", "text": heading.group(2)}])
                continue
            target = list_item.group(1) if list_item else line
            para_elements.append(_parse_inline_markdown(target))
            if i < len(lines) - 1:
                para_elements.append([{"tag": "text", "text": "\n"}])
        if para_elements:
            content["zh_cn"]["content"].append(para_elements)
    return content

def _parse_inline_markdown(text: str) -> list:
    elements = []
    for part in re.split(r'(\*\*.+?\*\*|`.+?`)', text):
        if part.startswith("**") and part.endswith("**"):
            elements.append({"tag": "b", "text": part[2:-2]})
        elif part.startswith("`") and part.endswith("`"):
            elements.append({"tag": "code", "text": part[1:-1]})
        elif part:
            elements.append({"tag": "text", "text": part})
    return elements

def send_message(client, chat_id: str, text: str) -> None:
    text = text.strip()
    if len(text) > 5000:
        send_message_text(client, chat_id, text)
        return
    try:
        if re.search(r'\*\*|`|^#{1,3}\s|^[-*]\s', text, re.MULTILINE):
            post = _build_post_content(text)
            req = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(chat_id).msg_type("post")
                    .content(json.dumps(post, ensure_ascii=False)).build()).build()
            resp = client.im.v1.message.create(req)
            if resp.success(): return
        send_message_text(client, chat_id, text)
    except Exception:
        try: send_message_text(client, chat_id, text)
        except Exception: pass

# ═══════════════════════════════════════════════════════════════════════
# Reaction
# ═══════════════════════════════════════════════════════════════════════

def add_reaction(client, message_id: str, emoji: str = "THINKING"):
    try:
        req = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji).build()).build()).build()
        resp = client.im.v1.message_reaction.create(req)
        return resp.data.reaction_id if resp.success() else None
    except Exception:
        return None

def delete_reaction(client, message_id: str, reaction_id):
    if not reaction_id: return
    try:
        req = DeleteMessageReactionRequest.builder() \
            .message_id(message_id).reaction_id(reaction_id).build()
        client.im.v1.message_reaction.delete(req)
    except Exception: pass

# ═══════════════════════════════════════════════════════════════════════
# 进度消息（agent 工具调用的过程反馈：先发一条，再原地编辑）
# ═══════════════════════════════════════════════════════════════════════

def send_progress_message(client, chat_id: str, text: str):
    """发送一条纯文本进度消息，返回 message_id（失败返回 None）。"""
    try:
        content = json.dumps({"text": text}, ensure_ascii=False)
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id).msg_type("text").content(content).build()).build()
        resp = client.im.v1.message.create(req)
        return resp.data.message_id if resp.success() else None
    except Exception:
        return None

def update_message(client, message_id: str, text: str) -> bool:
    """原地编辑一条消息（纯文本）。任何失败静默忽略。"""
    if not message_id: return False
    try:
        content = json.dumps({"text": text}, ensure_ascii=False)
        req = UpdateMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(UpdateMessageRequestBody.builder()
                .msg_type("text").content(content).build()).build()
        resp = client.im.v1.message.update(req)
        return resp.success()
    except Exception:
        return False

# ═══════════════════════════════════════════════════════════════════════
# 评论回复（agent 在评论流中的反馈通道）
# ═══════════════════════════════════════════════════════════════════════

def reply_to_comment(doc_token: str, comment_id: str, text: str) -> dict:
    """回复文档评论（bot 身份）。SDK 未封装创建回复接口，走 lark-cli 通用 API。"""
    body = {"content": {"elements": [{"type": "text_run", "text_run": {"content": text}}]}}
    return run_lark_cli([
        "api", "POST",
        f"/open-apis/drive/v1/files/{doc_token}/comments/{comment_id}/replies",
        "--params", json.dumps({"file_type": "docx"}),
        "--data", json.dumps(body, ensure_ascii=False),
    ])

# ═══════════════════════════════════════════════════════════════════════
# scheduler 专用的 lark-cli 函数
# ═══════════════════════════════════════════════════════════════════════

def search_meetings(start_date: str, end_date: str, query: str = "") -> list:
    args = ["vc", "+search", "--start", start_date, "--end", end_date]
    if query: args += ["--query", query]
    result = run_lark_cli(args, as_bot=False)
    if result.get("ok") is False: return []
    data = result.get("data", result)
    return data.get("meetings", data.get("items", [])) if isinstance(data, dict) else (data if isinstance(data, list) else [])

def get_meeting_minute_token(meeting_id: str) -> str:
    result = run_lark_cli(["vc", "+recording", "--meeting-ids", meeting_id])
    if result.get("ok") is False: return ""
    minutes = result.get("minutes", result.get("data", {}))
    if isinstance(minutes, dict):
        items = minutes.get("items", minutes.get("minutes", []))
        if isinstance(items, list) and items: return items[0].get("minute_token", "")
    return ""

def get_meeting_notes(minute_token: str) -> str:
    result = run_lark_cli(["vc", "+notes", "--minute-tokens", minute_token])
    if result.get("ok") is False: return f"[会议纪要获取失败: {result.get('error')}]"
    return json.dumps(result, ensure_ascii=False, indent=2)

def get_calendar_agenda(start: str = None, end: str = None) -> list:
    args = ["calendar", "+agenda"]
    if start: args += ["--start", start]
    if end: args += ["--end", end]
    result = run_lark_cli(args, as_bot=False)
    if result.get("ok") is False: return []
    data = result.get("data", result)
    return data.get("items", data.get("events", [])) if isinstance(data, dict) else (data if isinstance(data, list) else [])

def get_my_tasks() -> list:
    result = run_lark_cli(["task", "+get-my-tasks", "--page-all"], as_bot=False)
    if result.get("ok") is False: return []
    data = result.get("data", result)
    return data.get("items", data.get("tasks", [])) if isinstance(data, dict) else (data if isinstance(data, list) else [])

def create_document(title: str, content: str, wiki_space: str = None) -> dict:
    args = ["docs", "+create", "--title", title, "--markdown", content]
    if wiki_space: args += ["--wiki-space", wiki_space]
    return run_lark_cli(args, timeout=120)
