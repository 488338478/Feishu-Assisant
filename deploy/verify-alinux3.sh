#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_HOME=${AGENT_HOME:-/srv/agent}
REPO_DIR=${REPO_DIR:-$AGENT_HOME/assistant}
ENV_FILE=${ENV_FILE:-$AGENT_HOME/assistant.env}
MODE=${1:---live}
FAILURES=0
export PATH="$AGENT_HOME/npm/bin:$PATH"

ok() { echo "OK   $*"; }
bad() { echo "FAIL $*" >&2; FAILURES=$((FAILURES + 1)); }
has_real_value() {
  local key=$1 value
  value=$(sed -n "s/^${key}=//p" "$ENV_FILE" 2>/dev/null | tail -n 1)
  [[ -n $value && $value != *xxx* && $value != *CHANGE_ME* ]]
}
check_command() {
  command -v "$1" >/dev/null 2>&1 && ok "$1: $(command -v "$1")" || bad "$1 不可用"
}

echo "Assistant deployment verification ($MODE)"
[[ -r /etc/os-release ]] && grep -q '^ID="\?alinux"\?$' /etc/os-release \
  && ok "Alibaba Cloud Linux" || bad "操作系统不是 Alibaba Cloud Linux"
for command_name in git python3 npm claude lark-cli systemctl; do check_command "$command_name"; done
[[ -x "$AGENT_HOME/venv/bin/python" ]] && ok "Python venv" || bad "Python venv 缺失"
[[ -f "$REPO_DIR/main.py" ]] && ok "代码目录 $REPO_DIR" || bad "代码目录不完整"
[[ -f "$ENV_FILE" ]] && ok "环境文件存在且未输出其内容" || bad "环境文件缺失"
if id -u agent >/dev/null 2>&1 && runuser -u agent -- test -w "$AGENT_HOME/data"; then
  ok "agent 用户可写数据目录"
else
  bad "agent 用户不可写数据目录"
fi

if [[ -f "$ENV_FILE" && $MODE != "--install" ]]; then
  has_real_value FEISHU_APP_ID && ok "FEISHU_APP_ID 已配置" || bad "FEISHU_APP_ID 仍为空或为占位值"
  has_real_value FEISHU_APP_SECRET && ok "FEISHU_APP_SECRET 已配置" || bad "FEISHU_APP_SECRET 仍为空或为占位值"
fi

if [[ -x "$AGENT_HOME/venv/bin/python" && -f "$REPO_DIR/main.py" ]]; then
  (cd "$AGENT_HOME" && env ASSISTANT_DATA_DIR="$AGENT_HOME/data" \
    ASSISTANT_PROFILES_DIR="$REPO_DIR/profiles" \
    "$AGENT_HOME/venv/bin/python" -c "import lark_oapi, numpy; import assistant.main") \
    && ok "Python 依赖与应用导入" || bad "Python 依赖或应用导入失败"
fi

if [[ $MODE == "--tests" ]]; then
  TEST_DATA=$(mktemp -d)
  if (cd "$REPO_DIR" && env ASSISTANT_DATA_DIR="$TEST_DATA" \
    "$AGENT_HOME/venv/bin/python" -m unittest discover -s tests -v); then
    ok "完整测试"
  else
    bad "完整测试失败"
  fi
  rm -rf "$TEST_DATA"
fi

if [[ $MODE == "--live" ]]; then
  systemctl is-active --quiet assistant.service && ok "assistant.service active" || bad "assistant.service 未运行"
  systemctl is-enabled --quiet assistant.service && ok "assistant.service enabled" || bad "assistant.service 未启用"
  journalctl -u assistant.service -n 80 --no-pager | grep -q "缺少 FEISHU_APP_ID" \
    && bad "服务日志报告飞书凭证缺失" || ok "日志未发现凭证缺失错误"
fi

if (( FAILURES > 0 )); then
  echo "Verification failed: $FAILURES item(s)" >&2
  exit 1
fi
echo "Verification passed"
