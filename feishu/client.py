"""飞书 SDK + lark-cli 封装 — harness 侧仅保留 IM 发送 / reaction / 评论回复 / scheduler 所需函数。

文档/表格/会议等读写能力已全部移交 agent runtime（claude CLI + lark-cli skill），
Python 侧仅额外保留评论事件的精确定位、状态预检与结果投递。
"""
import json
import re
import subprocess
import threading

import lark_oapi as lark
from lark_oapi.core import AccessTokenType, BaseRequest, HttpMethod
from lark_oapi.api.im.v1 import *

from ..config import APP_ID, APP_SECRET, LARK_CLI_PROFILE, LARK_CLI_TIMEOUT
from .lark_command import resolve_lark_cli

# ═══════════════════════════════════════════════════════════════════════
# lark-cli subprocess（scheduler 与评论回复使用）
# ═══════════════════════════════════════════════════════════════════════

def run_lark_cli(args: list, timeout: int = LARK_CLI_TIMEOUT, as_bot: bool = True) -> dict:
    """运行 lark-cli 命令并返回解析后的 JSON。"""
    cmd = [
        resolve_lark_cli(), "--profile", LARK_CLI_PROFILE,
        "--as", "bot" if as_bot else "user",
    ]
    cmd += args

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0:
            try:
                failure = json.loads(stderr or stdout)
                if isinstance(failure, dict) and failure.get("ok") is False:
                    return failure
            except json.JSONDecodeError:
                pass
            return {"ok": False, "error": stderr[:500] or f"exit code {result.returncode}"}

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


def fetch_bot_open_id(client) -> str:
    """Return this application's bot open_id, or empty string to fail closed."""
    request = BaseRequest.builder() \
        .http_method(HttpMethod.GET) \
        .uri("/open-apis/bot/v3/info") \
        .token_types({AccessTokenType.TENANT}) \
        .build()
    try:
        response = client.request(request)
        if not response.success() or response.raw is None:
            return ""
        payload = json.loads(response.raw.content.decode("utf-8"))
        return str((payload.get("bot") or {}).get("open_id") or "")
    except Exception:
        return ""

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

def reply_to_comment(doc_token: str, comment_id: str, text: str, file_type: str = "docx") -> dict:
    """以 bot 身份回复指定评论；全文及已解决评论由 CLI 再次预检。"""
    return run_lark_cli([
        "drive", "+add-reply", "--token", doc_token, "--type", file_type,
        "--comment-id", comment_id, "--content",
        json.dumps([{"type": "text", "text": text}], ensure_ascii=False),
    ])


def _comment_data(result: dict) -> dict:
    if not isinstance(result, dict) or result.get("ok") is False:
        raise RuntimeError(f"评论接口失败: {str(result.get('error') if isinstance(result, dict) else result)[:300]}")
    data = result.get("data", result)
    if not isinstance(data, dict):
        raise RuntimeError("评论接口返回了非对象数据")
    return data


def read_comment_thread(doc_token: str, comment_id: str, file_type: str) -> dict:
    """确定性读取评论状态和全部回复；不能拿任意最后一条充当本次事件。"""
    base = ["--token", doc_token, "--type", file_type]
    data = _comment_data(run_lark_cli([
        "drive", "+batch-query-comments", *base, "--comment-ids", comment_id,
    ]))
    target = next((item for item in data.get("items", [])
                   if isinstance(item, dict) and item.get("comment_id") == comment_id), None)
    if target is None:
        raise RuntimeError("未找到事件对应的评论")
    # 无法回复的评论不再读取整条线程，更不启动推理。
    if target.get("is_whole") is not False or target.get("is_solved") is not False:
        return {**target, "replies": []}
    replies, page_token, seen_pages, seen_replies = [], "", set(), set()
    for _ in range(100):
        argv = ["drive", "+list-replies", *base, "--comment-id", comment_id, "--page-size", "100"]
        if page_token:
            argv.extend(["--page-token", page_token])
        page = _comment_data(run_lark_cli(argv))
        for item in page.get("items", []):
            if not isinstance(item, dict) or not item.get("reply_id"):
                raise RuntimeError("回复列表结构不完整")
            if item["reply_id"] not in seen_replies:
                replies.append(item)
                seen_replies.add(item["reply_id"])
        if page.get("has_more") is False:
            return {**target, "replies": replies}
        page_token = page.get("page_token")
        if not isinstance(page_token, str) or not page_token or page_token in seen_pages:
            raise RuntimeError("回复分页信息不完整或循环")
        seen_pages.add(page_token)
    raise RuntimeError("评论超过分页上限，未执行指令")

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
    args = [
        "docs", "+create", "--title", title,
        "--doc-format", "markdown", "--content", content,
    ]
    if wiki_space: args += ["--parent-token", wiki_space]
    return run_lark_cli(args, timeout=120)
