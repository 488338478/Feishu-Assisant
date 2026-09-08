"""Synchronize the lark-cli profile used by the assistant."""

import os
import subprocess
from collections.abc import Callable, Mapping


def sync_lark_profile(
    env: Mapping[str, str] = os.environ,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    app_id = env.get("FEISHU_APP_ID", "")
    secret = env.get("FEISHU_APP_SECRET", "")
    profile = env.get("LARK_CLI_PROFILE", "assistant-bot")
    if not app_id or not secret:
        raise RuntimeError("missing Feishu credentials")

    result = run(
        [
            "lark-cli",
            "config",
            "init",
            "--name",
            profile,
            "--app-id",
            app_id,
            "--app-secret-stdin",
            "--brand",
            "feishu",
        ],
        input=secret + "\n",
        text=True,
        capture_output=True,
        timeout=30,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "lark-cli config failed").replace(
            secret, "[REDACTED]"
        )
        raise RuntimeError(detail[:300])
