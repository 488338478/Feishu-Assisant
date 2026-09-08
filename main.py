"""主入口 — 启动飞书知识库助手 Bot。
运行方式:
    python -m assistant.main     （从包父目录运行，凭证走环境变量）
"""
import sys, os
from pathlib import Path

# 允许直接运行 python assistant/main.py
if __package__ is None:
    _parent = str(Path(__file__).resolve().parent.parent)
    if _parent not in sys.path:
        sys.path.insert(0, _parent)
    __package__ = "assistant"

import threading
import lark_oapi as lark

from .config import APP_ID, APP_SECRET
from .feishu.client import build_client, fetch_bot_open_id, send_message
from .feishu.lark_profile import sync_lark_profile
from .core.memory import memory
from .core.scheduler import scheduler
from .handlers import on_message, on_doc_comment, set_bot_open_id

def main():
    if not APP_ID or not APP_SECRET:
        print("[FATAL] 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量，见 deploy/env.example", flush=True)
        sys.exit(1)

    try:
        sync_lark_profile(os.environ)
    except Exception:
        print("[FATAL] lark-cli 配置同步失败", flush=True)
        sys.exit(1)
    print("[LARK-CLI] 配置同步成功", flush=True)

    client = build_client()
    bot_open_id = fetch_bot_open_id(client)
    set_bot_open_id(bot_open_id)
    if bot_open_id:
        print("[BOT] 群聊仅响应 @机器人", flush=True)
    else:
        print("[WARN] 无法获取机器人身份，群聊消息将全部忽略", flush=True)

    print("=" * 60, flush=True)
    print("飞书知识库助手 Bot 启动中...", flush=True)
    print(f"L6 自主行动: {'✅' if scheduler.config['enabled'] else '❌'}", flush=True)
    print(f"记忆条数: {len(memory.facts)}", flush=True)
    print("=" * 60, flush=True)

    # 启动调度器
    if scheduler.config["enabled"]:
        def push_cb(chat_id, text):
            try: send_message(build_client(), chat_id, text)
            except Exception as e: print(f"[PUSH] {e}", flush=True)
        scheduler.set_push_callback(push_cb)
        threading.Thread(target=scheduler.start, daemon=True).start()

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(on_message) \
        .register_p2_customized_event("drive.notice.comment_add_v1", on_doc_comment) \
        .register_p2_im_message_reaction_created_v1(lambda _: None) \
        .register_p2_im_message_reaction_deleted_v1(lambda _: None) \
        .build()

    ws_client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler)
    print("[READY] 助手已就绪", flush=True)
    try: ws_client.start()
    finally: scheduler.stop()

if __name__ == "__main__":
    main()
