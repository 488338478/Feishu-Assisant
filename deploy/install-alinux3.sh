#!/usr/bin/env bash
# Alibaba Cloud Linux 3 installer. Git is intentionally managed by the operator.
set -Eeuo pipefail

AGENT_HOME=${AGENT_HOME:-/srv/agent}
AGENT_USER=${AGENT_USER:-agent}
REPO_DIR=${REPO_DIR:-$AGENT_HOME/assistant}
START_SERVICE=0
CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION:-2.1.162}
[[ ${1:-} == "--start" ]] && START_SERVICE=1

fail() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }
env_value() {
  local key=$1 value first last
  value=$(sed -n "s/^${key}=//p" "$AGENT_HOME/assistant.env" 2>/dev/null | tail -n 1)
  value=${value%$'\r'}
  if (( ${#value} >= 2 )); then
    first=${value:0:1}
    last=${value: -1}
    if [[ $first == '"' && $last == '"' ]] || [[ $first == "'" && $last == "'" ]]; then
      value=${value:1:${#value}-2}
    fi
  fi
  printf '%s' "$value"
}

[[ $EUID -eq 0 ]] || fail "请使用 sudo bash deploy/install-alinux3.sh"
[[ -r /etc/os-release ]] || fail "无法识别操作系统"
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == "alinux" && ${VERSION_ID:-} == 3* ]] || \
  fail "仅支持 Alibaba Cloud Linux 3；当前 ID=${ID:-unknown} VERSION_ID=${VERSION_ID:-unknown}"
[[ -f "$REPO_DIR/requirements.txt" && -f "$REPO_DIR/main.py" ]] || \
  fail "代码必须位于 $REPO_DIR；请先由运维安装 Git 并 clone 仓库"
command -v git >/dev/null 2>&1 || fail "Git 尚未安装；按约定请先由运维安装 Git"

step "安装系统依赖（不安装或修改 Git）"
dnf install -y python3 python3-pip nodejs npm tar gzip
command -v python3 >/dev/null || fail "python3 安装失败"
command -v npm >/dev/null || fail "npm 安装失败"
NODE_MAJOR=$(node -p 'Number(process.versions.node.split(".")[0])')
if (( NODE_MAJOR < 18 )); then
  dnf module reset -y nodejs || true
  dnf module enable -y nodejs:20 || \
    fail "当前 Node $(node --version)，且系统没有 nodejs:20 module；请配置可信 Node 18+ 软件源后重跑"
  dnf install -y nodejs npm
  NODE_MAJOR=$(node -p 'Number(process.versions.node.split(".")[0])')
fi
(( NODE_MAJOR >= 18 )) || fail "Claude CLI 需要 Node.js 18+；当前版本 $(node --version)"

step "创建专用用户和持久化目录"
id -u "$AGENT_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$AGENT_USER"
install -d -o "$AGENT_USER" -g "$AGENT_USER" -m 0750 \
  "$AGENT_HOME/data" "$AGENT_HOME/home" "$AGENT_HOME/backups" "$AGENT_HOME/npm"

step "创建 Python 虚拟环境并安装依赖"
[[ -x "$AGENT_HOME/venv/bin/python" ]] || python3 -m venv "$AGENT_HOME/venv"
"$AGENT_HOME/venv/bin/python" -m pip install --upgrade pip
"$AGENT_HOME/venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

step "安装服务所需 CLI"
runuser -u "$AGENT_USER" -- env HOME="$AGENT_HOME/home" npm_config_prefix="$AGENT_HOME/npm" \
  npm install -g \
  @larksuite/cli@1.0.94 "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION"
export PATH="$AGENT_HOME/npm/bin:$PATH"
command -v lark-cli >/dev/null || fail "lark-cli 安装后仍不可用"
command -v claude >/dev/null || fail "claude CLI 安装后仍不可用"
[[ $(lark-cli --version) == *1.0.94* ]] || fail "lark-cli 版本不是 1.0.94"
[[ $(claude --version) == "$CLAUDE_CODE_VERSION"* ]] || fail "Claude Code 版本不是 $CLAUDE_CODE_VERSION"

step "创建环境文件并保护密钥"
if [[ ! -f "$AGENT_HOME/assistant.env" ]]; then
  install -o "$AGENT_USER" -g "$AGENT_USER" -m 0600 \
    "$REPO_DIR/deploy/env.example" "$AGENT_HOME/assistant.env"
  echo "已生成 $AGENT_HOME/assistant.env；启动前必须填写真实凭证"
else
  chown "$AGENT_USER:$AGENT_USER" "$AGENT_HOME/assistant.env"
  chmod 0600 "$AGENT_HOME/assistant.env"
fi
chown -R "$AGENT_USER:$AGENT_USER" "$AGENT_HOME/venv" "$AGENT_HOME/data" "$AGENT_HOME/home" "$AGENT_HOME/npm"

step "安装 systemd unit"
install -o root -g root -m 0644 "$REPO_DIR/deploy/assistant.service" /etc/systemd/system/assistant.service
P4_DROPIN_DIR=/etc/systemd/system/assistant.service.d
P4_DROPIN_FILE=$P4_DROPIN_DIR/p4-workspace.conf
P4_WORKSPACE_VALUE=$(env_value P4_WORKSPACE)
if [[ -n $P4_WORKSPACE_VALUE ]]; then
  [[ $P4_WORKSPACE_VALUE == /* ]] || fail "P4_WORKSPACE 必须是绝对路径"
  [[ $P4_WORKSPACE_VALUE =~ ^/[A-Za-z0-9._/@:+-]+$ ]] || \
    fail "P4_WORKSPACE 含 systemd drop-in 不支持的字符；请使用无空格的服务器路径"
  [[ -d $P4_WORKSPACE_VALUE ]] || fail "P4_WORKSPACE 目录不存在：$P4_WORKSPACE_VALUE"
  P4_WORKSPACE_REAL=$(realpath -e -- "$P4_WORKSPACE_VALUE")
  install -d -o root -g root -m 0755 "$P4_DROPIN_DIR"
  install -o root -g root -m 0644 /dev/null "$P4_DROPIN_FILE"
  {
    echo '[Service]'
    printf 'ReadWritePaths=%s\n' "$P4_WORKSPACE_VALUE"
    if [[ $P4_WORKSPACE_REAL != "$P4_WORKSPACE_VALUE" ]]; then
      printf 'ReadWritePaths=%s\n' "$P4_WORKSPACE_REAL"
    fi
  } > "$P4_DROPIN_FILE"
else
  rm -f -- "$P4_DROPIN_FILE"
fi
systemctl daemon-reload
systemctl enable assistant.service

if [[ $START_SERVICE -eq 1 ]]; then
  step "执行启动前验收"
  bash "$REPO_DIR/deploy/verify-alinux3.sh" --prestart
  step "启动服务"
  systemctl restart assistant.service
  sleep 3
  bash "$REPO_DIR/deploy/verify-alinux3.sh" --live
else
  step "执行安装态验收"
  bash "$REPO_DIR/deploy/verify-alinux3.sh" --install
  echo
  echo "安装完成但尚未启动。填写 $AGENT_HOME/assistant.env 后执行："
  echo "  sudo bash $REPO_DIR/deploy/install-alinux3.sh --start"
fi
