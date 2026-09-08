"""Single-flight lark-cli Device Flow authorization."""

import json
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class AuthResult:
    ok: bool
    message: str = ""


class LarkAuthManager:
    def __init__(
        self,
        profile: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: int = 300,
    ) -> None:
        self.profile = profile
        self.runner = runner
        self.timeout = timeout
        self._condition = threading.Condition()
        self._inflight = False
        self._generation = 0
        self._last_result = AuthResult(False, "飞书授权尚未开始。")

    def _run(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
        return self.runner(
            ["lark-cli", "--profile", self.profile, *args],
            text=True,
            capture_output=True,
            timeout=timeout,
        )

    @staticmethod
    def _json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        try:
            value = json.loads(result.stdout or "{}")
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    @classmethod
    def _find_string(cls, value: Any, keys: set[str]) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and item:
                    return item
            for item in value.values():
                found = cls._find_string(item, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_string(item, keys)
                if found:
                    return found
        return ""

    def _already_logged_in(self) -> bool:
        result = self._run(["auth", "status"])
        if result.returncode:
            return False
        return self._json(result).get("identity") == "user"

    def _authorize(self, domain: str, notify: Callable[[str], None]) -> AuthResult:
        try:
            if self._already_logged_in():
                return AuthResult(True)

            started = self._run(
                ["auth", "login", "--no-wait", "--json", "--domain", domain]
            )
            if started.returncode:
                combined = f"{started.stderr}\n{started.stdout}".lower()
                if "secret" in combined and "invalid" in combined:
                    return AuthResult(False, "飞书授权启动失败，请联系管理员检查应用凭证。")
                return AuthResult(False, "飞书授权启动失败，请稍后重试。")

            payload = self._json(started)
            url = self._find_string(
                payload,
                {"verification_uri_complete", "verification_url", "verification_uri", "url"},
            )
            device_code = self._find_string(payload, {"device_code", "deviceCode"})
            if not url or not device_code:
                return AuthResult(False, "飞书授权响应格式异常，请联系管理员更新 lark-cli。")

            notify(f"此操作需要你的飞书用户授权，请点击链接完成授权：\n{url}")
            finished = self._run(
                ["auth", "login", "--json", "--device-code", device_code],
                timeout=self.timeout,
            )
            if finished.returncode:
                return AuthResult(False, "飞书授权未完成或已过期，请重新发送原请求。")
            done = self._json(finished)
            if done.get("ok") is False:
                return AuthResult(False, "飞书授权未完成或已过期，请重新发送原请求。")
            return AuthResult(True)
        except subprocess.TimeoutExpired:
            return AuthResult(False, "飞书授权等待超时，请重新发送原请求。")
        except (OSError, subprocess.SubprocessError):
            return AuthResult(False, "无法运行 lark-cli，请联系管理员检查安装。")

    def ensure_user(self, domain: str, notify: Callable[[str], None]) -> AuthResult:
        """Ensure a reusable user login, sharing one concurrent Device Flow."""
        with self._condition:
            observed_generation = self._generation
            if self._inflight:
                while self._inflight and self._generation == observed_generation:
                    self._condition.wait()
                return self._last_result
            self._inflight = True

        result = AuthResult(False, "飞书授权失败，请稍后重试。")
        try:
            result = self._authorize(domain, notify)
            return result
        finally:
            with self._condition:
                self._last_result = result
                self._generation += 1
                self._inflight = False
                self._condition.notify_all()
