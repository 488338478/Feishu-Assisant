"""L4: Agent runtime — 每条飞书消息一次 claude CLI 调用。

- 按 chat_id 选择 profile（决定 cwd 与权限天花板，见 profiles/<name>/.claude/）
- 按 sender open_id 选择 tier（read/edit/submit），叠加 --disallowedTools 逐层收窄
- --resume 续会话；per-chat 锁保证会话链不交错
- 不再使用 bypassPermissions：未 allow 的工具在非交互模式下自动拒绝
- 全量调用落 audit_log.jsonl
"""
import json
import subprocess
import threading
import time

from pathlib import Path

from ..config import (
    CLAUDE_EXE, CLAUDE_TIMEOUT, SESSIONS_FILE, RUNTIME_CONFIG_FILE,
    AUDIT_LOG_FILE, PROFILES_DIR, P4_WORKSPACE, THESIS_DIR,
)
from ..core.memory import memory

# ═══════════════════════════════════════════════════════════════════════
# profile 与 tier
# ═══════════════════════════════════════════════════════════════════════

def _profile_cwd(profile: str) -> Path | None:
    """profile → 工作目录。None 表示该模式未配置。"""
    if profile == "docs":
        return PROFILES_DIR / "docs"
    if profile == "thesis":
        return Path(THESIS_DIR) if THESIS_DIR else None
    if profile == "dev":
        return Path(P4_WORKSPACE) if P4_WORKSPACE else None
    return None

PROFILE_TIMEOUT = {"dev": 900}  # 开发任务耗时长；其余用 CLAUDE_TIMEOUT

# 所有 tier 永久禁止的 P4 危险操作
_P4_DANGER = [
    "Bash(p4 revert:*)", "Bash(p4 delete:*)",
    "Bash(p4 undo:*)", "Bash(p4 resolve:*)",
]

# tier → 叠加的 --disallowedTools（deny 优先于 profile settings 的 allow）
TIER_DENY = {
    "read": ["Edit", "Write", "NotebookEdit",
             "Bash(p4 edit:*)", "Bash(p4 add:*)",
             "Bash(p4 submit:*)", "Bash(p4 sync:*)"] + _P4_DANGER,
    "edit": ["Bash(p4 submit:*)", "Bash(p4 sync:*)"] + _P4_DANGER,
    "submit": list(_P4_DANGER),
}

TIER_DESC = {
    "read": "只读：仓库分析/查日志/review 意见 + 飞书文档操作，不能改动任何文件",
    "edit": "改动：可在工作区修改代码（p4 edit/add），提交由人执行",
    "submit": "提交：可 p4 sync/submit，changelist 描述带飞书用户归因",
}

_DEFAULT_RUNTIME_CONFIG = {
    "default_profile": "docs",
    "chat_profiles": {},   # chat_id → profile 名
    "default_tier": "read",
    "user_tiers": {},      # open_id → tier
}

def _load_runtime_config() -> dict:
    """每次调用重新读（小文件），改配置免重启；文件缺失则生成默认。"""
    cfg = dict(_DEFAULT_RUNTIME_CONFIG)
    try:
        if RUNTIME_CONFIG_FILE.exists():
            with open(RUNTIME_CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            cfg.update({k: v for k, v in loaded.items() if k in cfg})
        else:
            with open(RUNTIME_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[RUNTIME] config error: {e}", flush=True)
    return cfg

def resolve_profile(chat_id: str) -> str:
    cfg = _load_runtime_config()
    return cfg["chat_profiles"].get(chat_id, cfg["default_profile"])

def resolve_tier(sender_id: str) -> str:
    cfg = _load_runtime_config()
    tier = cfg["user_tiers"].get(sender_id, cfg["default_tier"])
    return tier if tier in TIER_DENY else "read"

def describe_access(chat_id: str, sender_id: str) -> str:
    """「我的权限」命令。"""
    profile = resolve_profile(chat_id)
    tier = resolve_tier(sender_id)
    return ("## 你的权限\n"
            f"- 当前群模式：**{profile}**\n"
            f"- 你的层级：**{tier}** — {TIER_DESC[tier]}\n"
            "- 调整方式：管理员编辑数据目录下的 runtime_config.json（即时生效）")

# ═══════════════════════════════════════════════════════════════════════
# 会话持久化（chat_id → claude session_id）
# ═══════════════════════════════════════════════════════════════════════

sessions_lock = threading.RLock()

def _load_sessions() -> dict:
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v if isinstance(v, dict) else {"active": v, "history": []}
                for k, v in data.items()}
    except Exception:
        return {}

sessions: dict = _load_sessions()

def _save_sessions() -> None:
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[RUNTIME] sessions save error: {e}", flush=True)

def _get_chat_data(chat_id: str) -> dict:
    with sessions_lock:
        return sessions.setdefault(chat_id, {"active": None, "history": []})

def _add_to_history(chat_id: str, session_id: str) -> None:
    with sessions_lock:
        data = _get_chat_data(chat_id)
        data["history"] = [h for h in data["history"] if h["id"] != session_id]
        data["history"].insert(0, {"id": session_id, "title": session_id[:8],
                                   "time": int(time.time())})
        data["history"] = data["history"][:20]
        data["active"] = session_id
    _save_sessions()

# per-chat 锁：同一群的 resume 链不交错
_chat_locks: dict[str, threading.Lock] = {}

def _chat_lock(chat_id: str) -> threading.Lock:
    with sessions_lock:
        return _chat_locks.setdefault(chat_id, threading.Lock())

# ═══════════════════════════════════════════════════════════════════════
# 审计
# ═══════════════════════════════════════════════════════════════════════

def _audit(record: dict) -> None:
    try:
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[AUDIT] {e}", flush=True)

# ═══════════════════════════════════════════════════════════════════════
# CLI 调用（stream-json 流式：工具调用过程经 progress_cb 实时回报）
# ═══════════════════════════════════════════════════════════════════════

def _tool_brief(name: str, inp: dict) -> str:
    """把一次工具调用压成一行可读摘要。"""
    if name == "Bash":
        cmd = str(inp.get("command", "")).replace("\n", " ")
        return f"执行: {cmd[:80]}"
    if name in ("Read", "Edit", "Write"):
        return f"{name}: {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"{name}: {inp.get('pattern', '')}"
    return name or "未知工具"

def _run_claude(prompt: str, session_id: str | None, cwd: Path,
                extra_deny: list[str], timeout: int,
                progress_cb=None) -> tuple[str, str | None, int]:
    """返回 (结果文本, session_id, 工具调用次数)。超时抛 TimeoutError。"""
    cmd = [CLAUDE_EXE, "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if session_id:
        cmd += ["--resume", session_id]
    if extra_deny:
        cmd += ["--disallowedTools"] + extra_deny

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", cwd=str(cwd), bufsize=1)
    killed = {"v": False}
    def _kill():
        killed["v"] = True
        try: proc.kill()
        except Exception: pass
    watchdog = threading.Timer(timeout, _kill)
    watchdog.start()

    result_text, new_sid, last_text, tool_count = "", session_id, "", 0
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            if etype == "assistant":
                for block in (evt.get("message", {}).get("content") or []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_count += 1
                        if progress_cb:
                            try:
                                progress_cb(_tool_brief(block.get("name", ""),
                                                        block.get("input") or {}), tool_count)
                            except Exception:
                                pass
                    elif block.get("type") == "text" and block.get("text"):
                        last_text = block["text"]
            elif etype == "result":
                result_text = evt.get("result", "") or last_text
                new_sid = evt.get("session_id", new_sid)
        proc.wait()
    finally:
        watchdog.cancel()

    if killed["v"]:
        raise TimeoutError(f"claude 超时 ({timeout}s)")
    if not result_text:
        stderr = ""
        try: stderr = (proc.stderr.read() or "")[:500]
        except Exception: pass
        if proc.returncode not in (0, None):
            raise RuntimeError(stderr or f"claude exit code {proc.returncode}")
        raise RuntimeError(stderr or "Claude empty response")
    return result_text, new_sid, tool_count

def _build_prompt(text: str, sender_id: str, tier: str, profile: str) -> str:
    """消息前缀：动态用户信息 + 语义记忆召回（静态人设/规则在 profile 的 CLAUDE.md）。"""
    parts = [f"[当前用户] open_id={sender_id or '未知'} | profile={profile} | "
             f"权限层级={tier}（{TIER_DESC[tier]}）"]
    try:
        mem = memory.format_context(query=text, top_k=5)
        if mem:
            parts.append(mem)
    except Exception as e:
        print(f"[RUNTIME] memory: {e}", flush=True)
    parts.append(f"[用户消息]\n{text}")
    return "\n\n".join(parts)

# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

def run(chat_id: str, text: str, sender_id: str = "", profile: str | None = None,
        progress_cb=None) -> tuple[str, dict]:
    """处理一条消息：解析 profile/tier → 单次 CLI 调用。

    返回 (最终文本, stats{ok, tools, duration, err})。
    progress_cb(brief, n) 在每次工具调用时回调（可为 None）。
    """
    profile = profile or resolve_profile(chat_id)
    tier = resolve_tier(sender_id)
    cwd = _profile_cwd(profile)
    if cwd is None or not cwd.exists():
        return (f"当前群模式（{profile}）未配置或工作目录不存在，请联系管理员。"), \
               {"ok": False, "tools": 0, "duration": 0.0, "err": "profile_unavailable"}

    deny = TIER_DENY.get(tier, TIER_DENY["read"])
    timeout = PROFILE_TIMEOUT.get(profile, CLAUDE_TIMEOUT)
    prompt = _build_prompt(text, sender_id, tier, profile)

    t0 = time.time()
    ok, err, tools = True, "", 0
    with _chat_lock(chat_id):
        session_id = _get_chat_data(chat_id).get("active")
        try:
            print(f"[RUNTIME] profile={profile} tier={tier} chat={chat_id[:12]}...", flush=True)
            result, new_sid, tools = _run_claude(prompt, session_id, cwd, deny, timeout,
                                                 progress_cb=progress_cb)
            if new_sid:
                _add_to_history(chat_id, new_sid)
        except (TimeoutError, subprocess.TimeoutExpired):
            ok, err = False, "timeout"
            result = f"处理超时（{timeout}秒），请简化问题或稍后再试。"
        except Exception as e:
            ok, err = False, str(e)[:200]
            result = f"处理出错：{e}"
            print(f"[RUNTIME] error: {e}", flush=True)

    duration = round(time.time() - t0, 1)
    _audit({
        "time": int(t0), "chat_id": chat_id, "sender": sender_id,
        "profile": profile, "tier": tier, "ok": ok, "err": err,
        "duration_s": duration, "tools": tools, "text": text[:200],
    })
    return result, {"ok": ok, "tools": tools, "duration": duration, "err": err}
