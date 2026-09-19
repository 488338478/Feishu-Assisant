# 服务器 AI 部署手册：Alibaba Cloud Linux 3

这份文档供服务器上的 AI 或运维代理在仓库 clone 完成后逐条执行。目标环境是 Alibaba Cloud Linux 3、systemd 和固定目录 `/srv/agent/assistant`。Git 由管理员单独安装；部署脚本不会安装、升级或修改 Git。

从一台空服务器开始时，先把**真实 Git 仓库地址和分支/tag**交给服务器 AI，让它 clone 后读取仓库根目录的 `SERVER_DEPLOY.md`。服务器上已经存在仓库时，才可以直接要求它读取 `/srv/agent/assistant/deploy/AI_DEPLOY_ALINUX3.md`。

## 执行原则

- 不在聊天、终端回显或日志中打印 `assistant.env` 内容、飞书密钥和 API Key。
- 不关闭 SELinux 或防火墙。本服务只使用出站 HTTPS/WebSocket，不需要开放入站端口。
- 不删除 `/srv/agent/data`、`/srv/agent/home` 或 `/srv/agent/backups`。
- 升级前必须备份并记录当前 Git commit；失败时先保留日志和现场。
- 所有命令成功后才继续下一步，任何非零退出码都停止。

## 首次部署

管理员先安装 Git。当前部署仓库和分支已经固定；首次 clone 的完整提示词见仓库根目录 `SERVER_DEPLOY.md`。等价命令如下：

```bash
sudo mkdir -p /srv/agent
sudo chown "$USER":"$USER" /srv/agent
export REPO_URL='https://github.com/488338478/Feishu-Assisant.git'
export DEPLOY_REF='master'
git clone --branch "$DEPLOY_REF" --single-branch "$REPO_URL" /srv/agent/assistant
cd /srv/agent/assistant
git rev-parse HEAD
cat SERVER_DEPLOY.md
sudo bash deploy/install-alinux3.sh
```

安装脚本会通过 `dnf` 安装 Python、Node/npm、tar/gzip，创建 `agent` 用户和 venv，并以该用户安装已验证的 `lark-cli 1.0.94` 与 Claude Code `2.1.162` 到 `/srv/agent/npm`。它会注册 systemd，但在凭证仍是占位值时不会启动服务。Claude Code 官方要求 Node.js 18+，脚本会检查版本，并优先尝试 Alibaba Linux 的 `nodejs:20` module。

安全编辑配置：

```bash
sudoedit /srv/agent/assistant.env
sudo chmod 600 /srv/agent/assistant.env
sudo chown agent:agent /srv/agent/assistant.env
```

至少填写 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。如 Claude CLI 使用 API Key，再填写 `ANTHROPIC_API_KEY`；如果使用兼容 Anthropic 协议的网关，把 `ANTHROPIC_API_KEY` 清空，再填写 `ANTHROPIC_BASE_URL` 与 `ANTHROPIC_AUTH_TOKEN`。如果使用 Claude CLI 自身认证，API Key 保持为空并在 `HOME=/srv/agent/home` 的服务身份环境中完成认证。Claude Code 的官方安装与认证说明见 <https://docs.anthropic.com/en/docs/claude-code/getting-started>。

```bash
cd /srv/agent/assistant
sudo bash deploy/install-alinux3.sh --start
```

## 飞书控制台前置条件

应用需要机器人能力、长连接事件接收和正确可用范围。至少订阅：

- `im.message.receive_v1`
- `im.message.recalled_v1`
- 使用文档评论功能时订阅 `drive.notice.comment_add_v1`

群聊完整历史需要 `im:message.group_msg`。只有 `im:message.group_at_msg:readonly` 时，机器人通常只能看到 @它的消息。读取日历需要 `calendar:calendar.event:read`。权限或事件变更后必须发布新应用版本，再重新授权用户能力。

## 验收

先运行完整自动验收：

```bash
cd /srv/agent/assistant
sudo bash deploy/verify-alinux3.sh --tests
sudo bash deploy/verify-alinux3.sh --live
sudo systemctl status assistant.service --no-pager
sudo journalctl -u assistant.service -n 100 --no-pager
```

随后在飞书执行：

1. 私聊发送“我的权限”。
2. 目标群 @机器人发送“查看配置”。
3. 群成员先发送不 @机器人的三条普通消息，再 @机器人询问前文；确认能按 `[时间 姓名] 原文` 检索。
4. 私聊尝试要求读取某个群的记录，确认被拒绝。
5. 发送“清除上下文”，确认当前私聊或当前群共享上下文被清除。
6. 读取一次日历；若缺少 scope，应明确提示 `calendar:calendar.event:read`。

验收完成后记录 commit、时间、执行人、飞书应用版本和两条 verify 命令的结果。不得记录密钥。

## 升级

```bash
cd /srv/agent/assistant
OLD_COMMIT=$(git rev-parse HEAD)
echo "$OLD_COMMIT" | sudo tee /srv/agent/backups/pre-upgrade-commit.txt >/dev/null
git status --short
sudo systemctl stop assistant.service
sudo bash deploy/backup-alinux3.sh
git fetch --all --prune
git pull --ff-only
sudo bash deploy/install-alinux3.sh
sudo bash deploy/verify-alinux3.sh --tests
sudo systemctl start assistant.service
sudo bash deploy/verify-alinux3.sh --live
```

若 `git status --short` 非空，停止升级，先查明服务器是否存在人工修改；不要 reset、stash 或覆盖。

## 备份

```bash
sudo bash /srv/agent/assistant/deploy/backup-alinux3.sh
sudo ls -l /srv/agent/backups
```

备份包含数据、环境文件和 CLI/认证状态，含敏感信息，目录与压缩包权限为 `0700/0600`。脚本会在需要时短暂停止服务，并在备份完成后恢复原运行状态，以保证 SQLite 文件一致。应再复制到受控的加密备份位置，不要上传到代码仓库或普通对象存储桶。

## 回滚

应用升级失败且旧代码仍兼容当前数据时：

```bash
cd /srv/agent/assistant
sudo systemctl stop assistant.service
OLD_COMMIT=$(sudo cat /srv/agent/backups/pre-upgrade-commit.txt)
git status --short
git checkout "$OLD_COMMIT"
sudo bash deploy/install-alinux3.sh
sudo systemctl start assistant.service
sudo bash deploy/verify-alinux3.sh --live
```

如果升级已经迁移或损坏数据，保持服务停止，选择升级前的备份并先校验：

```bash
cd /srv/agent/backups
sha256sum -c assistant-<timestamp>.tar.gz.sha256
sudo mkdir -p /srv/agent/restore-review
sudo tar -xzf assistant-<timestamp>.tar.gz -C /srv/agent/restore-review
```

先检查 `/srv/agent/restore-review` 内容，再把明确需要的 `data`、`assistant.env` 或 `home` 恢复到原位置。不要未经检查直接覆盖。恢复后重新设置所有者为 `agent:agent`、环境文件权限为 `0600`，再启动和验收。

## AI 交付格式

服务器 AI 完成后只需报告：部署 commit、服务状态、测试数量与结果、飞书人工验收结果、备份文件路径，以及仍需管理员处理的权限项。不得粘贴环境文件、token、secret 或认证目录内容。
