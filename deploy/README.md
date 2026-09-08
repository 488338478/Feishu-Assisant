# 服务器部署指南（Phase 1/2）

目标：飞书助手运行在 P4V 代码仓库所在的 Linux 云服务器上，出站 WebSocket 连飞书，无需公网入站。

## 1. 目录规划

```
/srv/agent/
├── assistant/          # 本仓库（Python 包）
├── venv/               # Python 虚拟环境
├── data/               # 数据（会话/记忆/调度/runtime 配置/审计）
├── assistant.env       # 环境变量（cp deploy/env.example 填写）
└── depot/              # P4 工作区（dev 模式 cwd，可放在别处）
```

## 2. 依赖安装

```bash
# Python 侧
python3 -m venv /srv/agent/venv
/srv/agent/venv/bin/pip install lark-oapi numpy

# claude CLI（API 按量认证）
# 见 https://code.claude.com/docs/en/setup —— 安装后确认 `claude --version` 可用
# assistant.env 中可选配置 ANTHROPIC_API_KEY；不配置时由 Claude CLI 使用其原生认证配置

# lark-cli + profile 登录（bot 身份）
# 安装后：lark-cli auth login --profile assistant-bot  （按提示完成应用授权）

# p4 CLI（dev 模式）
# 安装 helix-cli，然后以 bot 的 P4 用户登录：
#   p4 -p <P4PORT> -u assistant-bot login
```

## 3. lark-cli 官方 skills（可选增强）

```bash
# 需要能访问 GitHub；不可达时跳过，各 profile 的 CLAUDE.md 已内置速查表
npx skills add larksuite/cli -g -a claude -s '*' -y
```

## 4. dev 模式：profile 拷入工作区 + P4 protections

```bash
# profiles/dev 的内容是模板，拷到 P4 工作区根目录，并填写 CLAUDE.md 中的 TODO
cp -r /srv/agent/assistant/profiles/dev/. /srv/depot/
# → /srv/depot/CLAUDE.md  +  /srv/depot/.claude/settings.json  +  /srv/depot/.claude/skills/p4v/
```

P4 服务端 protections（`p4 protect`，bot 账号无 super，最后防线）：

```
Protections:
	read user assistant-bot * //depot/...
	write user assistant-bot * //depot/dev/...
```

## 5. 配置人员与群权限

`data/runtime_config.json`（首次运行自动生成，改了即时生效）：

```json
{
  "default_profile": "docs",
  "chat_profiles": { "oc_开发群chat_id": "dev" },
  "default_tier": "read",
  "user_tiers": { "ou_张三open_id": "submit", "ou_李四open_id": "edit" }
}
```

获取 open_id / chat_id 的方法：让成员在群里随便发一句话，看 `data/audit_log.jsonl` 里的 `sender` / `chat_id` 字段。

## 6. 启动

```bash
sudo cp assistant/deploy/assistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now assistant
journalctl -u assistant -f   # 看日志
```

启动后在目标群发「推送到这里」设定每日摘要/提醒的目标群；发「我的权限」「查看配置」验证命令路径。

## 7. 本地开发（Windows）

```powershell
$env:FEISHU_APP_ID="cli_xxx"; $env:FEISHU_APP_SECRET="xxx"
$env:THESIS_DIR="C:\path\to\毕设"   # 想用 thesis 模式时
python -m assistant.main            # 从 assistant 包父目录运行
```
