"""配置常量 — 相对路径优先，部署差异全部走环境变量（见 deploy/env.example）。

代码内不出现硬编码绝对路径：
  FEISHU_APP_ID / FEISHU_APP_SECRET   飞书应用凭证（必填，缺失则启动失败）
  ANTHROPIC_API_KEY                   可选；不由 harness 读取或注入，交给 Claude CLI 按其原生优先级处理
  CLAUDE_EXE                          claude CLI 路径（默认 "claude"，走 PATH）
  ASSISTANT_DATA_DIR                  数据目录（默认包内 data/）
  ASSISTANT_PROFILES_DIR              agent profile 目录（默认包内 profiles/）
  P4_WORKSPACE                        P4V 工作区根目录（dev profile 的 cwd；未设置则 dev 模式不可用）
  THESIS_DIR                          毕设项目目录（thesis profile 的 cwd；未设置则 thesis 模式不可用）
"""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent

# 飞书应用凭证 — 只从环境变量读取
APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

# CLI
CLAUDE_EXE = os.environ.get("CLAUDE_EXE", "claude")
LARK_CLI_PROFILE = os.environ.get("LARK_CLI_PROFILE", "assistant-bot")
BOT_IDS = tuple(value.strip() for value in
                os.environ.get("ASSISTANT_BOT_IDS", "").split(",") if value.strip())

# 数据目录（默认包内 data/，首次运行自动创建）
BASE_DIR = Path(os.environ.get("ASSISTANT_DATA_DIR", str(PACKAGE_DIR / "data")))
BASE_DIR.mkdir(parents=True, exist_ok=True)
SESSIONS_FILE = BASE_DIR / "assistant_sessions.json"
MEMORY_FILE = BASE_DIR / "memory_store.json"
SCHEDULER_CONFIG_FILE = BASE_DIR / "scheduler_config.json"
RUNTIME_CONFIG_FILE = BASE_DIR / "runtime_config.json"
AUDIT_LOG_FILE = BASE_DIR / "audit_log.jsonl"
GROUP_HISTORY_DB_FILE = BASE_DIR / "group_history.sqlite3"
GROUP_HISTORY_CONFIG_FILE = BASE_DIR / "group_history_config.json"

# Agent profile 目录与各 profile 工作目录
PROFILES_DIR = Path(os.environ.get("ASSISTANT_PROFILES_DIR", str(PACKAGE_DIR / "profiles")))
P4_WORKSPACE = os.environ.get("P4_WORKSPACE", "")
THESIS_DIR = os.environ.get("THESIS_DIR", "")

# 超时与限制
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "1000"))
LARK_CLI_TIMEOUT = 60
MEMORY_MAX_FACTS = 500
