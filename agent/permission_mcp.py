"""Minimal stdio MCP server used by Claude Code's permission-prompt-tool."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


def _reply(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = {"code": -32600, "message": str(error)}
    else:
        payload["result"] = result
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _permission_result(decision: str, message: str = "") -> dict:
    if decision == "allow":
        return {"behavior": "allow"}
    return {"behavior": "deny", "message": message or "飞书管理员拒绝了这次工具调用。"}


def _wait_for_decision(arguments: dict) -> dict:
    directory = Path(os.environ["ASSISTANT_PERMISSION_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    request_path = directory / f"request-{token}.json"
    response_path = directory / f"response-{token}.json"
    payload = {
        "token": token,
        "chat_id": os.environ.get("ASSISTANT_PERMISSION_CHAT_ID", ""),
        "sender_id": os.environ.get("ASSISTANT_PERMISSION_SENDER_ID", ""),
        "profile": os.environ.get("ASSISTANT_PERMISSION_PROFILE", ""),
        "tier": os.environ.get("ASSISTANT_PERMISSION_TIER", ""),
        "chat_type": os.environ.get("ASSISTANT_PERMISSION_CHAT_TYPE", "p2p"),
        "tool_name": str(arguments.get("tool_name", "")),
        "tool_input": arguments.get("tool_input") or {},
        "created_at": time.time(),
    }
    request_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        deadline = time.time() + 300
        while time.time() < deadline:
            if response_path.is_file():
                response = json.loads(response_path.read_text(encoding="utf-8"))
                return _permission_result(response.get("decision", "deny"), response.get("message", ""))
            time.sleep(0.2)
        return _permission_result("deny", "飞书权限确认已超时。")
    finally:
        for path in (request_path, response_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def main() -> None:
    for line in os.sys.stdin:
        try:
            request = json.loads(line)
            method = request.get("method")
            request_id = request.get("id")
            if method == "initialize":
                _reply(request_id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "feishu-permission", "version": "1.0"},
                })
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                _reply(request_id, {"tools": [{
                    "name": "prompt",
                    "description": "Ask the Feishu administrator to approve one Claude tool call.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "tool_name": {"type": "string"},
                            "tool_input": {"type": "object"},
                        },
                        "required": ["tool_name", "tool_input"],
                    },
                }]})
            elif method == "tools/call":
                arguments = request.get("params", {}).get("arguments", {})
                decision = _wait_for_decision(arguments)
                _reply(request_id, {"content": [{
                    "type": "text", "text": json.dumps(decision, ensure_ascii=False)
                }]})
            else:
                _reply(request_id, error=f"unsupported MCP method: {method}")
        except Exception as exc:
            _reply(locals().get("request_id"), error=exc)


if __name__ == "__main__":
    main()
