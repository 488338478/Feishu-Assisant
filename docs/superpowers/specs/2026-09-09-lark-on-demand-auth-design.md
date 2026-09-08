# 飞书按需用户授权设计

日期：2026-09-09

## 目标

让飞书助手默认使用 bot 身份。只有操作确实需要用户身份且当前授权不可用时，才发起 OAuth 设备授权，并把授权链接发送到当前飞书会话。Windows 与 Linux 使用相同行为，不自动打开浏览器。

## 身份选择

- 默认使用 `--as bot`。
- 个人资源操作使用 `--as user`，包括“我的日程”“我的任务”“我的会议”等明确依赖用户身份的请求。
- 普通文档、Wiki、消息和应用级资源优先使用 bot 身份。
- 不因用户请求含糊而预先切换 user；先尝试 bot，只有 API 明确要求用户身份或缺少用户 token 时才进入授权流程。

## 授权流程

1. Agent 执行业务命令，优先使用 bot 身份。
2. 若业务明确要求 user，或 lark-cli 返回用户未登录/缺少 user scope，Python 授权管理器提取所需 domain 或 scope。
3. 授权管理器固定使用 `assistant-bot` profile 执行 `auth login --no-wait`。
4. 将 verification URL 发送到触发请求的飞书会话，不自动打开本机浏览器。
5. 用户点击链接并在飞书页面确认授权。
6. 授权管理器使用同一 profile 和 device code 等待授权完成。
7. 成功后自动重试原业务请求一次。
8. 失败、拒绝或过期时，向飞书返回明确状态，不循环创建新授权。

## 安全与并发

- Python 代码统一管理登录；禁止 Claude Agent 直接执行 `lark-cli auth login`、`auth logout` 或自行处理 device code。
- 授权命令必须显式携带 `--profile assistant-bot`。
- 飞书消息只发送 verification URL，不发送 app secret、access token、refresh token 或 device code。
- 同一 profile 同一时间只允许一个授权流程；并发请求复用现有链接。
- 授权链接设置过期状态；过期后由下一次确有需要的请求重新发起。
- 授权完成后由 lark-cli 持久化并刷新 token，业务代码不保存 token。

## 用户反馈

- 发起授权：发送“此操作需要你的飞书用户授权，请点击链接完成授权”。
- 等待授权：原请求保持处理中，但不重复刷屏。
- 授权成功：提示授权完成，并自动继续原请求。
- 授权拒绝或过期：结束请求，提示用户重新发送原命令以再次授权。
- 非授权类错误：直接返回原错误，不误触发 OAuth。

## 代码边界

- 新增独立授权管理模块，负责识别认证错误、发起设备流、维护单例授权状态和完成授权。
- `handlers.py` 负责把授权链接及状态发回触发会话。
- `agent/runtime.py` 继续负责 Claude CLI 生命周期，不保存飞书 OAuth 凭证。
- `profiles/docs/CLAUDE.md` 改为 bot-first 身份规则。
- `profiles/docs/.claude/settings.json` 禁止 Agent 直接执行登录、退出和 device-code 命令。

## 测试

- 无需用户身份的操作不会发起授权。
- 明确的个人资源操作优先使用 user，但仅在 token 不可用时发链接。
- 认证错误能生成一个授权链接，并固定使用 `assistant-bot` profile。
- 并发认证请求复用同一授权流程。
- 授权成功后原操作只重试一次。
- 拒绝、超时和非认证错误不会进入无限重试。
- 飞书消息和审计日志不包含 device code、token 或 secret。

## 非目标

- 不自动批准飞书 OAuth；最终同意仍由用户在飞书页面完成。
- 不在 Windows 自动打开浏览器。
- 不修改飞书开放平台后台的应用权限。
- 不把 user 身份设为所有命令的默认身份。
