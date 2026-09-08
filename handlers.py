"""事件处理器 — IM 消息 + 文档评论。推理全部走 agent.runtime（claude CLI）。"""
import json
import threading
import time

from lark_oapi.api.im.v1 import *

from .feishu.client import (
    build_client, send_message, add_reaction, delete_reaction, reply_to_comment,
    send_progress_message, update_message,
)
from .agent import runtime
from .config import LARK_CLI_PROFILE
from .core.memory import memory
from .core.scheduler import scheduler, PUSH_TARGET_MARKER
from .feishu.auth import LarkAuthManager
from .feishu.identity import auth_required_domain, required_user_domain


auth_manager = LarkAuthManager(LARK_CLI_PROFILE)

# ═══════════════════════════════════════════════════════════════════════
# 消息处理
# ═══════════════════════════════════════════════════════════════════════

def process_message(text: str, chat_id: str, sender_id: str, client, message_id: str = "") -> str:
    # 调度配置命令：确定性指令，直接处理，不走 agent
    cfg_result = scheduler.configure(text)
    if cfg_result:
        if cfg_result == PUSH_TARGET_MARKER:
            return scheduler.set_push_target(chat_id)
        return cfg_result

    # 权限查询命令
    if any(kw in text for kw in ["我的权限", "查看权限"]):
        return runtime.describe_access(chat_id, sender_id)

    # 个人资源才预检 user OAuth；共享资源保持 bot-first。
    user_domain = required_user_domain(text)
    if user_domain:
        auth_result = auth_manager.ensure_user(
            user_domain,
            lambda message: send_message(client, chat_id, message),
        )
        if not auth_result.ok:
            return auth_result.message

    # 进度消息：首次工具调用时发出，之后节流原地编辑，收尾改成完成摘要
    progress = {"mid": None, "events": [], "last_edit": 0.0}

    def progress_cb(brief: str, n: int) -> None:
        progress["events"].append(brief)
        now = time.time()
        if now - progress["last_edit"] < 3:
            return
        progress["last_edit"] = now
        body = "⏳ 处理中…\n" + "\n".join(f"🔧 {e}" for e in progress["events"][-5:])
        if progress["mid"] is None:
            progress["mid"] = send_progress_message(client, chat_id, body)
        else:
            update_message(client, progress["mid"], body)

    # agent 处理
    response, stats = runtime.run(chat_id, text, sender_id=sender_id,
                                  progress_cb=progress_cb)

    # bot-first 调用若确认必须使用 user，由 Python 统一授权并只重试一次。
    fallback_domain = auth_required_domain(response)
    if fallback_domain:
        auth_result = auth_manager.ensure_user(
            fallback_domain,
            lambda message: send_message(client, chat_id, message),
        )
        if not auth_result.ok:
            return auth_result.message
        retry_response, retry_stats = runtime.run(
            chat_id, text, sender_id=sender_id, progress_cb=progress_cb
        )
        response = retry_response
        stats = {
            **retry_stats,
            "tools": stats.get("tools", 0) + retry_stats.get("tools", 0),
            "duration": stats.get("duration", 0) + retry_stats.get("duration", 0),
        }
    if progress["mid"]:
        update_message(client, progress["mid"],
                       f"✅ 完成：工具调用 {stats['tools']} 次，耗时 {stats['duration']:.0f}s")

    # 自动学习（从对话提取项目事实入库）
    try:
        memory.auto_learn(text, response)
    except Exception:
        pass

    return response

# ═══════════════════════════════════════════════════════════════════════
# 事件回调
# ═══════════════════════════════════════════════════════════════════════

def on_message(data: P2ImMessageReceiveV1) -> None:
    print(f"[MSG] received", flush=True)
    msg = data.event.message
    chat_id, message_id = msg.chat_id, msg.message_id
    try: content = json.loads(msg.content)
    except Exception: return
    text = content.get("text", "").strip()
    if not text: return

    # 提问人身份（三层权限按此解析）
    try: sender_id = data.event.sender.sender_id.open_id or ""
    except Exception: sender_id = ""

    client = build_client()
    def run():
        rid = add_reaction(client, message_id, "THINKING")
        try:
            result = process_message(text, chat_id, sender_id, client, message_id)
            delete_reaction(client, message_id, rid)
            send_message(client, chat_id, result)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            delete_reaction(client, message_id, rid)
            send_message(client, chat_id, f"处理出错：{e}")
    threading.Thread(target=run, daemon=True).start()

def on_doc_comment(event) -> None:
    print(f"[DOC] {json.dumps(event.event, ensure_ascii=False, default=str)[:500]}", flush=True)
    try:
        ev = event.event
        comment = ev.get("comment", {})
        comment_id = comment.get("comment_id", "")
        ct = ""
        for item in comment.get("content", []):
            if isinstance(item, dict):
                for elem in item.get("elements", []):
                    if elem.get("type") == "text_run":
                        ct += elem.get("text_run", {}).get("content", "")
        if not ct: return

        is_mentioned = any(kw in ct.lower() for kw in
            ["@bot", "@助手", "@assistant", "@知识库助手", "修改", "编辑", "更新", "帮我看", "帮我查", "优化", "润色"])
        if not is_mentioned: return

        doc_token = ev.get("object", {}).get("obj_token", ev.get("object", {}).get("owner_id", ""))
        if not doc_token: return

        # 交给 agent（docs profile）：自读文档、自行完成修改，最终文本回贴评论
        prompt = (
            f"飞书用户在文档（token={doc_token}）的评论中提出请求：「{ct}」。\n"
            f"请用 lark-cli 读取该文档，判断意图并完成相应处理（追加/替换/润色/回答问题）。\n"
            f"你的最终回复将作为评论回复发给用户：改动了就简要说明做了什么；"
            f"只是建议或未改动文档，直接给出建议内容。"
        )
        response, _ = runtime.run("doc_" + doc_token[:8], prompt, sender_id="", profile="docs")

        if comment_id:
            res = reply_to_comment(doc_token, comment_id, response[:2000])
            if res.get("ok") is False:
                print(f"[DOC] comment reply failed: {res.get('error')}", flush=True)
    except Exception as e:
        print(f"[DOC] error: {e}", flush=True)
