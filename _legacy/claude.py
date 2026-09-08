"""L3+L4: Plan-aware Agentic Tool Loop — Claude 调用引擎。"""
import json
import subprocess
import threading

from ..config import CLAUDE_EXE, CLAUDE_TIMEOUT, MAX_TOOL_ROUNDS, MAX_TOOLS_PER_ROUND, SESSIONS_FILE
from ..system_prompt import SYSTEM_PROMPT
from ..feishu.tools import execute_tool, extract_tools_and_clean
from .planner import parse_plan, format_plan_progress, build_plan_context

sessions_lock = threading.RLock()  # RLock 可重入，防止 deadlock

def _load_sessions_helper():
    import json as _j
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = _j.load(f)
        return {k: v if isinstance(v, dict) else {"active": v, "history": [{"id": v, "title": v[:8], "time": 0}]}
                for k, v in data.items()}
    except: return {}

sessions: dict = _load_sessions_helper()

def _save_sessions():
    import json as _j
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        _j.dump(sessions, f, ensure_ascii=False, indent=2)

def get_chat_data(chat_id: str) -> dict:
    with sessions_lock:
        return sessions.setdefault(chat_id, {"active": None, "history": []})

def add_to_history(chat_id: str, session_id: str) -> None:
    from datetime import datetime
    with sessions_lock:
        data = get_chat_data(chat_id)
        data["history"] = [h for h in data["history"] if h["id"] != session_id]
        data["history"].insert(0, {"id": session_id, "title": session_id[:8],
                                    "time": int(datetime.now().timestamp())})
        data["history"] = data["history"][:20]
        data["active"] = session_id
    _save_sessions()

def _run_claude(conversation: str, session_id: str | None) -> tuple[str, str | None]:
    cmd = [CLAUDE_EXE, "-p", conversation, "--output-format", "json",
           "--permission-mode", "bypassPermissions", "--system-prompt", SYSTEM_PROMPT]
    if session_id: cmd += ["--resume", session_id]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", timeout=CLAUDE_TIMEOUT)
    stdout = (result.stdout or "").strip()
    if not stdout:
        raise RuntimeError((result.stderr or "Claude empty response")[:500])
    data = json.loads(stdout)
    return data.get("result", ""), data.get("session_id")

def call_claude(prompt: str, chat_id: str, client, extra_context: str = "") -> str:
    """Plan-aware agentic tool loop (L3+L4)。"""
    try:
        with sessions_lock:
            session_id = get_chat_data(chat_id).get("active")

        full_prompt = prompt
        if extra_context:
            full_prompt = (
                f"[以下是与用户问题相关的知识库和项目上下文]\n\n{extra_context}\n\n"
                f"[用户消息]\n{prompt}"
            )

        conversation = full_prompt
        used_session = session_id
        active_plan = []
        plan_phase = False

        for rnd in range(MAX_TOOL_ROUNDS):
            print(f"[CLAUDE] round {rnd+1}/{MAX_TOOL_ROUNDS} plan={'Y' if active_plan else 'N'}", flush=True)
            response_text, new_sid = _run_claude(conversation, used_session)
            if new_sid: used_session = new_sid
            print(f"[CLAUDE] resp_len={len(response_text)}", flush=True)

            plans, clean = parse_plan(response_text)
            tools, clean = extract_tools_and_clean(clean or response_text)

            # --- 新计划 ---
            if plans and not active_plan:
                active_plan = plans
                plan_phase = True
                print(f"[PLAN] {len(active_plan)} steps", flush=True)
                conversation += f"\n\n{format_plan_progress(active_plan)}\n请开始执行步骤1。"
                continue

            # --- 计划执行 ---
            if plan_phase and active_plan:
                cur = next((s for s in active_plan if s["status"] == "pending"), None)
                if cur: cur["status"] = "running"

                if tools:
                    for s in active_plan:
                        if s["status"] == "running":
                            s["status"] = "done"
                            round_results = []
                            for tn, tp in tools[:MAX_TOOLS_PER_ROUND]:
                                tr = execute_tool(tn, tp, client)
                                s["result"] = str(tr)[:500]
                                round_results.append(f"[工具结果: {tn}]\n{tr}\n[/工具结果]")
                                print(f"[PLAN] step{s['num']} {tn} done", flush=True)
                            conversation += f"\n\n" + "\n\n".join(round_results) + f"\n\n{build_plan_context(active_plan)}"
                            break
                    continue

                if all(s["status"] == "done" for s in active_plan) and not tools:
                    plan_phase = False
                    if used_session: add_to_history(chat_id, used_session)
                    return clean or response_text

                if not tools:
                    conversation += f"\n\n{build_plan_context(active_plan)}"
                    continue

            # --- 普通工具 ---
            if tools and not plan_phase:
                results = []
                for tn, tp in tools[:MAX_TOOLS_PER_ROUND]:
                    tr = execute_tool(tn, tp, client)
                    results.append(f"[工具结果: {tn}]\n{tr}\n[/工具结果]")
                    print(f"[TOOL] {tn} done", flush=True)
                conversation += f"\n\n" + "\n\n".join(results) + \
                    "\n\n[系统提示] 以上是工具执行结果。请继续回答。"
                continue

            # --- 最终答案 ---
            if not tools and not plan_phase:
                if used_session: add_to_history(chat_id, used_session)
                return clean or response_text

        # 超限
        if active_plan:
            response_text, new_sid = _run_claude(
                conversation + "\n\n[系统提示] 达到最大轮数，请给出最终总结。", used_session)
            if new_sid: add_to_history(chat_id, new_sid)
            return response_text
        return "抱歉，处理超时。请简化问题。"

    except subprocess.TimeoutExpired:
        return "处理超时（300秒），请简化问题。"
    except Exception as e:
        print(f"[CLAUDE] {e}", flush=True)
        return f"处理出错：{e}"
