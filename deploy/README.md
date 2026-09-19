# 服务器部署指南

当前标准服务器环境为 **Alibaba Cloud Linux 3 + systemd**。供服务器 AI 直接执行的完整流程见 [AI_DEPLOY_ALINUX3.md](AI_DEPLOY_ALINUX3.md)。Git 由管理员安装，仓库部署脚本不会管理 Git。

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

## 2. 一键安装

```bash
cd /srv/agent/assistant
sudo bash deploy/install-alinux3.sh
sudoedit /srv/agent/assistant.env
sudo bash deploy/install-alinux3.sh --start
```

脚本幂等执行，安装 Python/Node 依赖、固定版本 `@larksuite/cli@1.0.94`、`@anthropic-ai/claude-code@2.1.162`、systemd unit 和数据目录。两个 npm CLI 都安装在 `agent` 用户专用的 `/srv/agent/npm`，不会用 root npm 全局目录。dev 模式的 p4 CLI 仍由管理员单独安装和登录。

## 3. lark-cli 官方 skills（可选增强）

```bash
# 从飞书官方 well-known 入口同步 skills（无需 GitHub）。
npx skills add https://open.feishu.cn -g -y
```

后续升级优先运行 `lark-cli update`，它会同步更新 CLI 和 AI skills。升级后重新启动运行助手的 Agent/服务，使新版命令说明生效。

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

同时在 `/srv/agent/assistant.env` 至少设置 `P4_WORKSPACE`，并按实际环境设置
`P4PORT`、`P4USER`、`P4CLIENT`。systemd 不会继承管理员登录 shell 里的这些变量；
ticket 必须由运行服务的 `agent` 用户读取。配置后验证：

```bash
sudo -u agent env HOME=/srv/agent/home \
  P4PORT="实际地址" P4USER="实际用户" P4CLIENT="实际客户端" \
  p4 -d /srv/depot info
sudo -u agent env HOME=/srv/agent/home \
  P4PORT="实际地址" P4USER="实际用户" P4CLIENT="实际客户端" \
  p4 -d /srv/depot opened
```

助手通过 P4 CLI 和工作区文件读取仓库，并不远程操作 P4V 图形界面。
安装脚本会根据 `P4_WORKSPACE` 自动生成 systemd drop-in，将工作区加入
`ReadWritePaths`。修改工作区路径后必须重新运行 `install-alinux3.sh --start`；只重启
服务不会更新 systemd 写白名单。`verify-alinux3.sh --live` 会检查 agent 用户写权限、
dev profile 的 Write/Edit 配置和 systemd 白名单。

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
sudo bash /srv/agent/assistant/deploy/install-alinux3.sh --start
sudo bash /srv/agent/assistant/deploy/verify-alinux3.sh --live
sudo journalctl -u assistant -f
```

启动后在目标群发「推送到这里」设定每日摘要/提醒的目标群；发「我的权限」「查看配置」验证命令路径。

## 7. 本地开发（Windows）

```powershell
$env:FEISHU_APP_ID="cli_xxx"; $env:FEISHU_APP_SECRET="xxx"
$env:THESIS_DIR="C:\path\to\毕设"   # 想用 thesis 模式时
python -m assistant.main            # 从 assistant 包父目录运行
```
