"""评论后台处理：按文档串行，按评论线程续会话，结果由 Python 投递。"""
import hashlib
from pathlib import Path
import threading
import time

from ..agent import runtime
from ..config import BASE_DIR
from .client import read_comment_thread, reply_to_comment, _comment_data
from .doc_comment import CommentNotice, candidate, parse_notice, reply_gate, extract_content, comment_prompt
from .comment_store import CommentStore

REPLY_MAX = 2000
# 固定数量的锁避免随文档数增长；同文档的读取、修改和投递全过程串行。
_DOC_LOCKS = tuple(threading.Lock() for _ in range(64))


def _doc_lock(event: CommentNotice):
    digest = hashlib.sha256((event.file_type + ":" + event.doc_token).encode()).digest()
    return _DOC_LOCKS[int.from_bytes(digest[:2], "big") % len(_DOC_LOCKS)]


class CommentService:
    def __init__(self, path: Path):
        self.store = CommentStore(path)

    def handle(self, event: CommentNotice, bot_ids: tuple[str, ...], attempts: int = 3) -> None:
        if not candidate(event, bot_ids):
            return
        with _doc_lock(event):
            for attempt in range(attempts):
                if not self._handle_locked(event, bot_ids):
                    return
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)

    def _handle_locked(self, event: CommentNotice, bot_ids: tuple[str, ...]) -> bool | None:
        state, saved_response = self.store.claim(event.key)
        if state in {"done", "reading", "running"}:
            print(f"[DOC] ignored state={state} event={event.event_id}", flush=True)
            return
        try:
            thread = read_comment_thread(event.doc_token, event.comment_id, event.file_type)
            if thread.get("is_whole") is not False or thread.get("is_solved") is not False:
                print("[DOC] ignored: whole/solved/unknown comment state", flush=True)
                self.store.set_state(event.key, "done")
                return
            replies = thread["replies"]
            target = next((item for item in replies if item.get("reply_id") == event.reply_id), None)
            if target is None:
                raise RuntimeError("触发回复尚不可读；等待重投，不执行其他回复")
            if state == "result":
                self._deliver(event, saved_response or "", replies, bot_ids)
                return
            gate = reply_gate(event, replies, bot_ids)
            if not gate:
                print(f"[DOC] ignored: no mention/followup event={event.event_id}", flush=True)
                self.store.set_state(event.key, "done")
                return
            print(f"[DOC] accepted gate={gate} author={event.author_id} event={event.event_id}", flush=True)
            text, _ = extract_content(target.get("content"))
            if text:
                # 先落盘；进程若在修改文档后崩溃，重投不能再次执行写操作。
                self.store.set_state(event.key, "running")
                state = "running"
                response, _ = runtime.run(event.session_id, comment_prompt(event, thread),
                                          sender_id=event.author_id, profile="docs")
            else:
                response = "请补充具体文字指令，例如：帮我润色这段内容。"
            response = response.strip() or "未获得可用结果，请补充指令后重试。"
            if response.startswith("[LARK_USER_AUTH_REQUIRED:"):
                response = "当前机器人权限不足，无法完成此操作。请检查机器人对文档的访问权限。"
            if len(response) > REPLY_MAX:
                response = response[:REPLY_MAX - 20] + "\n（内容较长，可继续追问详情。）"
            self.store.set_state(event.key, "result", response)
            state = "result"
            self._deliver(event, response, replies, bot_ids)
        except Exception as exc:
            if state == "new":
                self.store.set_state(event.key, "received")
            print(f"[DOC] failed state={state} event={event.event_id}: {str(exc)[:300]}", flush=True)
            return state in {"new", "result"}

    def _deliver(self, event: CommentNotice, response: str, replies: list[dict], bot_ids: tuple[str, ...]):
        index = next(i for i, item in enumerate(replies) if item.get("reply_id") == event.reply_id)
        baseline = self.store.delivery_baseline(event.key, [item["reply_id"] for item in replies])
        # HTTP 超时也可能已经提交：跨过人类插入的回复，且不把旧的同文回复算作本轮投递。
        if baseline is not None:
            for item in replies[index + 1:]:
                if (item.get("reply_id") not in baseline and item.get("user_id") in bot_ids
                        and extract_content(item.get("content"))[0] == response):
                    self.store.set_state(event.key, "done")
                    return
        result = _comment_data(reply_to_comment(event.doc_token, event.comment_id, response, event.file_type))
        if not result.get("reply_id"):
            raise RuntimeError("回复结果缺少 reply_id，保留待核对状态")
        self.store.set_state(event.key, "done")


_service = None
_service_lock = threading.Lock()
_active_lock = threading.Lock()
_active: set[str] = set()


def dispatch_doc_comment(payload, event_id: str, bot_ids: tuple[str, ...]) -> None:
    event = parse_notice(payload, event_id)
    print(f"[DOC] received type={event.notice_type} mentioned={event.is_mentioned} "
          f"author={event.author_id} to={event.to_ids} event={event.event_id}", flush=True)
    if not candidate(event, bot_ids):
        print("[DOC] ignored: invalid event, identity or trigger", flush=True)
        return
    # 回调只登记内存占位并起线程，不在 SDK 事件循环里做数据库、CLI 或推理 I/O。
    with _active_lock:
        if event.key in _active:
            return
        _active.add(event.key)

    def work():
        global _service
        try:
            with _service_lock:
                if _service is None:
                    _service = CommentService(BASE_DIR / "doc_comments.sqlite3")
            _service.handle(event, bot_ids)
        except Exception as exc:
            print(f"[DOC] worker failed: {str(exc)[:300]}", flush=True)
        finally:
            with _active_lock:
                _active.discard(event.key)
    try:
        threading.Thread(target=work, daemon=True, name="doc-comment").start()
    except Exception:
        with _active_lock:
            _active.discard(event.key)
        raise
