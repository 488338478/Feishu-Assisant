# 飞书团队 Agent 当前架构图

## 1. 总体架构

```mermaid
flowchart TB
    User["飞书用户 / 群聊"]
    Feishu["飞书开放平台<br/>Bot + WebSocket"]
    Main["assistant/main.py<br/>启动 Bot 与 WebSocket 客户端"]
    Handler["handlers.py<br/>消息解析、命令处理、进度消息"]
    Runtime["agent/runtime.py<br/>每条消息一次 claude -p 调用"]

    Config["运行时配置<br/>data/runtime_config.json"]
    Profile["Profile 路由<br/>按 chat_id 选择 docs / thesis / dev"]
    Tier["权限路由<br/>按 sender open_id 选择 read / edit / submit"]

    Docs["profiles/docs<br/>CLAUDE.md + .claude/settings.json"]
    Thesis["profiles/thesis<br/>CLAUDE.md + .claude/settings.json"]
    Dev["profiles/dev<br/>CLAUDE.md + .claude/settings.json<br/>p4v skill"]

    Claude["本机 Claude CLI<br/>claude -p --resume<br/>stream-json"]
    Auth["Claude CLI 原生认证<br/>环境变量 / settings / CLI 配置"]
    Tools["Claude 工具层<br/>Read / Edit / Write / Bash<br/>lark-cli / p4"]
    FeishuTools["飞书文档工具<br/>lark-cli profile assistant-bot"]
    P4["P4 工作区<br/>仅 dev profile"]
    ThesisDir["毕设项目目录<br/>仅 thesis profile"]

    Memory["core/memory.py<br/>持久记忆 / 语义召回"]
    Vector["core/vector_store.py<br/>向量存储"]
    Sessions["data/assistant_sessions.json<br/>chat_id → Claude session_id"]
    Audit["data/audit_log.jsonl<br/>消息、用户、工具调用审计"]

    Scheduler["core/scheduler.py<br/>定时 Agent 任务 / agent_jobs"]
    ScheduleConfig["data/scheduler_config.json"]
    Push["主动推送回飞书群"]
    Env["环境变量<br/>FEISHU_APP_ID / FEISHU_APP_SECRET<br/>可选 ANTHROPIC_API_KEY<br/>CLAUDE_EXE / 路径配置"]

    User --> Feishu --> Main --> Handler --> Runtime
    Runtime --> Config --> Profile
    Config --> Tier
    Profile --> Docs
    Profile --> Thesis
    Profile --> Dev
    Tier --> Runtime
    Runtime --> Memory --> Vector
    Runtime --> Sessions
    Runtime --> Audit
    Runtime --> Claude
    Env --> Main
    Env --> Claude
    Auth --> Claude
    Claude --> Tools
    Tools --> FeishuTools
    Tools --> P4
    Tools --> ThesisDir
    Main --> Scheduler --> ScheduleConfig
    Scheduler --> Push --> Feishu
```

## 2. 普通消息处理时序

```mermaid
sequenceDiagram
    participant U as 飞书用户
    participant F as 飞书 WebSocket
    participant H as handlers.py
    participant R as runtime.py
    participant C as Claude CLI
    participant T as 工具层
    participant X as 飞书 / P4 / 本地文件

    U->>F: 发送消息
    F->>H: 推送事件
    H->>R: 文本 + chat_id + sender open_id
    R->>R: 读取 profile 与权限 tier
    R->>R: 加载记忆与历史 session
    R->>C: claude -p --resume<br/>stream-json
    C->>C: 使用原生环境/settings 认证
    C->>T: 调用 Read / Bash / Edit 等工具
    T->>X: 访问飞书、P4 或本地工作区
    C-->>R: 流式进度与最终结果
    R-->>H: 结果文本
    H-->>F: 回复或编辑进度消息
    F-->>U: 展示结果
```

## 3. Profile 与权限隔离

```mermaid
flowchart LR
    Chat["chat_id"] --> Profile["Profile"]
    Sender["sender open_id"] --> Tier["权限层级"]

    Profile --> Docs["docs<br/>只读知识库"]
    Profile --> Thesis["thesis<br/>毕设目录"]
    Profile --> Dev["dev<br/>P4 工作区"]

    Tier --> Read["read<br/>禁止 Edit / Write / P4 修改"]
    Tier --> Edit["edit<br/>允许修改，禁止 submit"]
    Tier --> Submit["submit<br/>允许提交，但禁止危险 P4 操作"]

    Profile -.权限天花板.-> Tier
    Tier -.叠加 --disallowedTools.-> Claude["Claude CLI"]
```

## 4. 认证链路

```mermaid
flowchart LR
    FeishuCreds["FEISHU_APP_ID<br/>FEISHU_APP_SECRET"]
    FeishuCreds --> FeishuWS["飞书 WebSocket"]

    ClaudeNative["Claude CLI 原生认证<br/>环境变量 / settings / CLI 配置"]
    ClaudeNative --> ClaudeCLI["claude -p"]

    LarkAuth["lark-cli profile assistant-bot<br/>飞书文档工具认证"]
    LarkAuth --> LarkCLI["lark-cli"]

    ClaudeCLI --> LarkCLI
```

## 5. 关键说明

- 飞书入口使用 WebSocket，不是业务层主动轮询。
- 每条飞书消息都会调用一次本机 Claude CLI，并通过 `--resume` 续接群聊会话。
- `chat_id` 决定 profile，`sender open_id` 决定权限层级。
- Claude API Key 不由项目代码注入；Claude CLI 使用自己的原生认证顺序。
- 飞书 App 凭证和 Claude 凭证是两套独立配置。
- `data/` 保存会话、记忆、调度配置和审计日志。
