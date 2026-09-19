"""事件处理器 — IM 消息 + 文档评论。推理全部走 agent.runtime（claude CLI）。"""
import json
import re
import threading
import time

from lark_oapi.api.im.v1 import *

from .feishu.client import (
    build_client, send_message, add_reaction, delete_reaction, reply_to_comment,
    send_progress_message, update_message,
)
from .agent import runtime
from .config import LARK_CLI_PROFILE, BOT_IDS
from .feishu.comment_service import dispatch_doc_comment
from .core.memory import memory
from .core.group_history import (
    GroupMessage,
    HistoryQuery,
    MessageNotFound,
    group_history_config,
    group_history_service,
    group_history_store,
)
from .core.scheduler import scheduler, PUSH_TARGET_MARKER
from .feishu.auth import LarkAuthManager
from .feishu.identity import auth_required_domain, required_user_domain
from .feishu.message_gate import message_targets_bot, strip_bot_mention
from .feishu.group_history_protocol import parse_history_request


auth_manager = LarkAuthManager(LARK_CLI_PROFILE)
BOT_OPEN_ID = ""

_SESSION_CONTROL_COMMANDS = {"清除上下文", "清除会话", "重置会话", "新建会话", "切换会话"}
_SESSION_CONTROL_INTENTS = (
    "清除上下文", "清空上下文", "清除会话", "重置会话", "重新开始",
    "新建会话", "切换会话", "换个话题", "忘掉前面", "忘记前面",
    "不要记得", "别记得",
)

_MODEL_QUERY_INTENTS = ("当前模型", "查看模型", "查看当前模型", "模型列表", "现在用什么模型", "现在是什么模型", "正在用什么模型")
_MODEL_SWITCH_PREFIXES = ("切换模型", "更换模型", "换模型", "切换到模型", "使用模型")
_MODEL_FUZZY_INTENTS = ("想换模型", "想换个模型", "想切换模型", "想更换模型", "怎么切模型", "如何切换模型")

_P4_ACTION_WORDS = (
    "进入", "打开", "看看", "查看", "读取", "检索", "搜索", "检查", "分析",
    "列出", "有哪些", "有啥", "状态", "提交", "变更", "文件", "代码", "仓库",
    "工作区", "changelist",
)
_P4_DOC_WORDS = ("飞书", "知识库", "文档", "说明", "教程", "操作手册")
_P4_REPOSITORY_WORDS = ("仓库", "工作区", "代码", "changelist", "提交", "变更", "状态")


def _requires_dev_profile(text: str) -> bool:
    """识别明确的 P4 工作区操作；飞书中的 P4 文档查询仍交给 docs。"""
    lowered = text.lower()
    compact = "".join(lowered.split())
    has_p4 = bool(re.search(r"p4v|perforce|(?<![a-z0-9])p4(?![a-z0-9])", lowered))
    has_repository_request = any(
        phrase in compact for phrase in ("仓库代码", "代码仓库", "p4工作区", "perforce工作区")
    )
    if not has_p4 and not has_repository_request:
        return False
    explicit_doc_request = any(word in compact for word in _P4_DOC_WORDS)
    explicit_repository_request = any(word in compact for word in _P4_REPOSITORY_WORDS)
    if explicit_doc_request and not explicit_repository_request:
        return False
    return has_repository_request or any(word in compact for word in _P4_ACTION_WORDS)


def _dev_profile_guidance(chat_id: str) -> str:
    return (
        "当前会话未配置为研发模式，无法读取 P4 工作区；本次不会改为搜索飞书。\n"
        "请管理员完成两项配置：\n"
        "1. 在 `/srv/agent/assistant.env` 设置 `P4_WORKSPACE` 为服务器上的 P4 client root；\n"
        "2. 在 `/srv/agent/data/runtime_config.json` 的 `chat_profiles` 中添加 "
        f"`\"{chat_id}\": \"dev\"`。\n"
        "环境变量变更后重启 `assistant.service`；群配置即时生效。配置完成后发送「我的权限」确认。"
    )


def _session_control_guidance(text: str) -> str | None:
    normalized = "".join(text.lower().split())
    mentions_context_reset = (
        any(word in normalized for word in ("上下文", "会话", "前面", "之前"))
        and any(word in normalized for word in ("清除", "清空", "重置", "忘掉", "忘记", "重新"))
    )
    if mentions_context_reset or any(intent in normalized for intent in _SESSION_CONTROL_INTENTS):
        return (
            "如果你希望从空白上下文重新开始，请单独发送「清除上下文」或「新建会话」。"
            "群聊中执行后会清除该群共享的上下文；私聊只清除当前私聊。"
        )
    return None


def _model_guidance(detail: str = "") -> str:
    suffix = f"\n{detail}" if detail else ""
    return (
        "只支持以下两个模型，请发送明确指令：\n"
        "「切换模型 DeepSeek 4.1」\n"
        "「切换模型 DeepSeek 4.0 Pro」\n"
        "也可以发送「当前模型」查看当前选择。"
        f"{suffix}"
    )


def _parse_model_command(text: str) -> tuple[str, str | None] | None:
    normalized = "".join(text.lower().split())
    if normalized in _MODEL_QUERY_INTENTS:
        return "query", None
    prefix = next((item for item in _MODEL_SWITCH_PREFIXES
                   if normalized.startswith(item)), None)
    if prefix:
        value = normalized[len(prefix):].lstrip("：:=-")
        value = value.removeprefix("为").removeprefix("到").removeprefix("成")
        aliases = {
            "deepseek4.1": "deepseek-v4-flash", "deepseek41": "deepseek-v4-flash",
            "4.1": "deepseek-v4-flash", "41": "deepseek-v4-flash", "flash": "deepseek-v4-flash",
            "deepseek4.0pro": "deepseek-v4-pro", "deepseek40pro": "deepseek-v4-pro",
            "4.0pro": "deepseek-v4-pro", "40pro": "deepseek-v4-pro", "pro": "deepseek-v4-pro",
        }
        return "switch", aliases.get(value, "")
    if any(intent in normalized for intent in _MODEL_FUZZY_INTENTS):
        return "fuzzy", None
    return None


def set_bot_open_id(open_id: str) -> None:
    global BOT_OPEN_ID
    BOT_OPEN_ID = open_id

# ═══════════════════════════════════════════════════════════════════════
# 消息处理
# ═══════════════════════════════════════════════════════════════════════

def process_message(
    text: str, chat_id: str, sender_id: str, client,
    message_id: str = "", chat_type: str = "p2p",
    reply_to: str = "", thread_id: str = "",
) -> str:
    if text.strip().lower() in _SESSION_CONTROL_COMMANDS:
        runtime.clear_session(chat_id, sender_id, chat_type)
        return "已清除当前会话上下文和关联记忆。"
    session_guidance = _session_control_guidance(text)
    if session_guidance:
        return session_guidance

    model_command = _parse_model_command(text)
    if model_command:
        action, model = model_command
        if action == "query":
            return runtime.describe_model(chat_id, chat_type)
        if action == "fuzzy":
            return _model_guidance()
        if not model:
            return _model_guidance("未识别这个模型名称。")
        runtime.set_model(chat_id, model, chat_type)
        return f"已将当前会话模型切换为 **{runtime.MODEL_LABELS[model]}**（`{model}`）。后续请求生效。"

    # 调度配置命令：确定性指令，直接处理，不走 agent
    cfg_result = scheduler.configure(text)
    if cfg_result:
        if cfg_result == PUSH_TARGET_MARKER:
            return scheduler.set_push_target(chat_id)
        return cfg_result

    # 权限查询命令
    if any(kw in text for kw in ["我的权限", "查看权限"]):
        return runtime.describe_access(chat_id, sender_id)

    # P4 工作区是 dev profile 的能力边界。未配置时直接说明，避免模型把 P4V
    # 误判成飞书资料名并调用 wiki/drive/docs 搜索。
    if _requires_dev_profile(text) and runtime.resolve_profile(chat_id) != "dev":
        return _dev_profile_guidance(chat_id)

    # 预检明确要求 user 的操作；支持 bot 的共享资源仍保持 bot-first。
    user_domain = required_user_domain(text)
    runtime_user_domain = user_domain
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
    response, stats = runtime.run(
        chat_id,
        text,
        sender_id=sender_id,
        progress_cb=progress_cb,
        force_user_domain=user_domain,
        chat_type=chat_type,
        group_reply_to=reply_to,
        group_thread_id=thread_id,
    )

    # bot-first 调用若确认必须使用 user，由 Python 统一授权并只重试一次。
    fallback_domain = auth_required_domain(response)
    if fallback_domain:
        auth_result = auth_manager.ensure_user(
            fallback_domain,
            lambda message: send_message(client, chat_id, message),
            force=True,
        )
        if not auth_result.ok:
            return auth_result.message
        retry_response, retry_stats = runtime.run(
            chat_id,
            text,
            sender_id=sender_id,
            progress_cb=progress_cb,
            force_user_domain=fallback_domain,
            chat_type=chat_type,
            group_reply_to=reply_to,
            group_thread_id=thread_id,
        )
        if auth_required_domain(retry_response):
            response = (
                "飞书授权已完成，但用户身份调用仍未成功。"
                "请稍后重试；若持续出现，请联系管理员检查应用权限。"
            )
        else:
            response = retry_response
            runtime_user_domain = fallback_domain
        stats = {
            **retry_stats,
            "tools": stats.get("tools", 0) + retry_stats.get("tools", 0),
            "duration": stats.get("duration", 0) + retry_stats.get("duration", 0),
        }

    history_request = parse_history_request(response)
    if history_request:
        if chat_type != "group":
            response = "当前请求不能读取群聊记录，请在对应群聊中重新提问。"
        else:
            max_rounds = group_history_config.load().max_search_rounds
            rounds = 0
            while history_request and rounds < max_rounds:
                rounds += 1
                try:
                    if history_request.kind == "search":
                        history_result = group_history_service.search(
                            chat_id,
                            HistoryQuery(
                                query=history_request.query,
                                time_range_hours=history_request.time_range_hours,
                                sender_ids=history_request.sender_ids,
                                thread_id=history_request.thread_id or thread_id or reply_to,
                                limit=history_request.limit,
                                exclude_message_ids=(message_id,) if message_id else (),
                            ),
                        )
                    else:
                        history_result = group_history_service.expand(
                            chat_id, history_request.message_id, history_request.part
                        )
                    history_context = history_result.text
                    runtime.audit_event({
                        "event": (
                            "group_history_search"
                            if history_request.kind == "search"
                            else "group_history_expand"
                        ),
                        "chat_id": chat_id,
                        "sender": sender_id,
                        "query": history_request.query[:200],
                        "message_ids": list(history_result.message_ids),
                        "estimated_tokens": history_result.estimated_tokens,
                        "truncated": history_result.truncated,
                        "round": rounds,
                        "policy": "current_group_only",
                    })
                except MessageNotFound:
                    history_context = "[检索结果] 请求的消息在当前群中不可用。"
                    runtime.audit_event({
                        "event": "group_history_denied",
                        "chat_id": chat_id,
                        "sender": sender_id,
                        "round": rounds,
                        "policy": "current_group_only",
                    })
                retry_response, retry_stats = runtime.run(
                    chat_id,
                    text,
                    sender_id=sender_id,
                    progress_cb=progress_cb,
                    force_user_domain=runtime_user_domain,
                    chat_type=chat_type,
                    group_history_context=history_context,
                    group_reply_to=reply_to,
                    group_thread_id=thread_id,
                )
                stats = {
                    **retry_stats,
                    "tools": stats.get("tools", 0) + retry_stats.get("tools", 0),
                    "duration": stats.get("duration", 0) + retry_stats.get("duration", 0),
                }
                response = retry_response
                history_request = parse_history_request(response)
            if history_request:
                final_response, final_stats = runtime.run(
                    chat_id,
                    text,
                    sender_id=sender_id,
                    progress_cb=progress_cb,
                    force_user_domain=runtime_user_domain,
                    chat_type=chat_type,
                    group_history_context=history_context,
                    group_reply_to=reply_to,
                    group_thread_id=thread_id,
                    group_history_exhausted=True,
                )
                stats = {
                    **final_stats,
                    "tools": stats.get("tools", 0) + final_stats.get("tools", 0),
                    "duration": stats.get("duration", 0) + final_stats.get("duration", 0),
                }
                response = final_response
                if parse_history_request(response):
                    response = "已达到本次回答的群聊记录检索上限，请缩小时间范围后重试。"
    if progress["mid"]:
        update_message(client, progress["mid"],
                       f"✅ 完成：工具调用 {stats['tools']} 次，耗时 {stats['duration']:.0f}s")

    # 自动学习（从对话提取项目事实入库）
    try:
        memory.auto_learn(text, response,
                          scope=runtime.session_key(chat_id, sender_id, chat_type))
    except Exception:
        pass

    return response

# ═══════════════════════════════════════════════════════════════════════
# 事件回调
# ═══════════════════════════════════════════════════════════════════════

def on_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    chat_id, message_id = msg.chat_id, msg.message_id
    try: content = json.loads(msg.content)
    except Exception: return
    raw_text = content.get("text", "")
    if not isinstance(raw_text, str):
        raw_text = ""
    content_type = getattr(msg, "message_type", "text") or "text"
    captured_content = raw_text if content_type == "text" else msg.content

    # 提问人身份（三层权限按此解析）
    try: sender_id = data.event.sender.sender_id.open_id or ""
    except Exception: sender_id = ""

    if msg.chat_type == "group" and captured_content and chat_id and message_id:
        try:
            event_sender_name = getattr(data.event.sender, "name", "") or ""
            sender_name = (
                event_sender_name
                or group_history_config.load().sender_names.get(sender_id, "")
                or sender_id
                or "未知成员"
            )
            group_history_store.record(GroupMessage(
                message_id=message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                sender_name=sender_name,
                sent_at_ms=int(getattr(msg, "create_time", 0) or time.time() * 1000),
                reply_to=getattr(msg, "parent_id", "") or "",
                thread_id=getattr(msg, "root_id", "") or "",
                content_type=content_type,
                content=captured_content,
            ))
        except Exception as error:
            print(f"[GROUP_HISTORY] capture error: {error}", flush=True)

    if not message_targets_bot(msg.chat_type, msg.mentions, BOT_OPEN_ID):
        return

    if not raw_text:
        return

    print(f"[MSG] received", flush=True)
    text = strip_bot_mention(raw_text, msg.mentions, BOT_OPEN_ID)
    if not text: return

    client = build_client()
    def run():
        rid = add_reaction(client, message_id, "THINKING")
        try:
            result = process_message(
                text, chat_id, sender_id, client, message_id, msg.chat_type,
                getattr(msg, "parent_id", "") or "",
                getattr(msg, "root_id", "") or "",
            )
            delete_reaction(client, message_id, rid)
            send_message(client, chat_id, result)
        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            delete_reaction(client, message_id, rid)
            send_message(client, chat_id, f"处理出错：{e}")
    threading.Thread(target=run, daemon=True).start()

def on_doc_comment(event) -> None:
    """SDK 回调只做候选筛选；读评论、推理和投递均在后台执行。"""
    try:
        # BOT_OPEN_ID 未解析成功时，额外 ID 不能代替启动身份验证。
        if not BOT_OPEN_ID:
            return
        header = getattr(event, "header", None)
        dispatch_doc_comment(getattr(event, "event", None),
                             getattr(header, "event_id", "") or "",
                             (BOT_OPEN_ID, *BOT_IDS))
    except Exception as e:
        print(f"[DOC] error: {e}", flush=True)


def on_message_recalled(data) -> None:
    """Remove recalled messages from the current-group history store."""
    try:
        event = data.event
        chat_id = getattr(event, "chat_id", "") or ""
        message_id = getattr(event, "message_id", "") or ""
        if chat_id and message_id:
            group_history_store.remove(chat_id, message_id)
    except Exception as error:
        print(f"[GROUP_HISTORY] recall error: {error}", flush=True)
