#!/usr/bin/env bash
# 飞书助手服务器一键初始化（Ubuntu/Debian 系，以 deploy/README.md 为准）
# 用法：sudo bash setup.sh
set -euo pipefail

AGENT_HOME=${AGENT_HOME:-/srv/agent}
AGENT_USER=${AGENT_USER:-agent}
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # assistant 包目录

echo "==> 1/6 创建用户与目录"
id -u "$AGENT_USER" &>/dev/null || useradd -r -m -s /bin/bash "$AGENT_USER"
mkdir -p "$AGENT_HOME/data"
cp -r "$PKG_DIR" "$AGENT_HOME/"

echo "==> 2/6 Python 依赖"
[ -d "$AGENT_HOME/venv" ] || python3 -m venv "$AGENT_HOME/venv"
"$AGENT_HOME/venv/bin/pip" install -q -r "$AGENT_HOME/assistant/requirements.txt"

echo "==> 3/6 检查外部 CLI"
for c in claude lark-cli p4; do
  if command -v "$c" &>/dev/null; then echo "  ✅ $c: $(command -v $c)"
  else echo "  ⚠️  $c 未安装（见 deploy/README.md 第 2 节）"; fi
done

echo "==> 4/6 环境变量文件"
if [ ! -f "$AGENT_HOME/assistant.env" ]; then
  cp "$AGENT_HOME/assistant/deploy/env.example" "$AGENT_HOME/assistant.env"
  echo "  已生成 $AGENT_HOME/assistant.env —— 请编辑填入凭证"
else
  echo "  已存在，跳过"
fi

echo "==> 5/6 systemd 服务"
cp "$AGENT_HOME/assistant/deploy/assistant.service" /etc/systemd/system/
systemctl daemon-reload
echo "  已注册（尚未启动）"

echo "==> 6/6 权限"
chown -R "$AGENT_USER:$AGENT_USER" "$AGENT_HOME"

cat <<'EOF'

完成。后续步骤：
  1. 编辑 /srv/agent/assistant.env 填入 FEISHU_APP_ID/SECRET；ANTHROPIC_API_KEY 仅在需要显式使用 API Key 时填写
  2. 安装已验证的 lark-cli：npm install -g @larksuite/cli@1.0.94（bot profile 启动时自动同步）
  3. 同步官方 skills：npx skills add https://open.feishu.cn -g -y
  4. dev 模式：cp -r profiles/dev/. <P4工作区>/，填写 CLAUDE.md TODO，配置 p4 protect
  5. sudo systemctl enable --now assistant && journalctl -u assistant -f
  6. 群里发「推送到这里」「我的权限」验证；从 data/audit_log.jsonl 取 open_id 填 runtime_config.json
EOF
