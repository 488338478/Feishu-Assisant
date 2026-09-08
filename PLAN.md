# 飞书 Agent 架构重构 PLAN（v2）— 待 Review

> 状态：待批准。批准前不动代码（`_legacy/` 迁移与 `config.py` 重写已发生，见文末「已落地改动」）。

## 1. 背景与目标

从「本地知识库问答 bot」重构为**部署在代码仓库云服务器上的团队 Agent**，服务两类人：

| 人群 | 需求 | 对应能力 |
|---|---|---|
| 非代码人员 | 飞书文档/表格的增删改查；不指明文档时 agent 主动搜索判断 | lark-cli（skill 化） |
| 代码人员 | 与服务器 P4V 仓库联动：筛查 bug、产日志、加 feature、总结架构、代码审查 | Claude Code 原生工具 + p4 CLI |

核心设计决策（已与需求方确认）：
1. **删除全部自研协议**：`build_proactive_context` 关键词预取、`[TOOL]`/`[PLAN]` 文本协议、正则解析——全部废弃。
2. **Claude Code CLI 作为唯一 agent runtime**：原生 agent loop、原生工具调用、`--resume` 续会话。每条飞书消息 = 一次 CLI 调用。
3. **飞书能力 = lark-cli + skill**，经 CLI 的 Bash 工具调用，不再手写 Python 工具包装。
4. **按人分三层权限**：读 / 改动 / submit，飞书里不同人发消息可调用的深度不同。

## 2. 总体架构

```
飞书用户 ──WS──> Python harness（薄，只做飞书事件与路由）
                   ├─ main.py        WS 接入
                   ├─ handlers.py    消息/评论路由 + 调度命令 + "我的权限"命令
                   ├─ agent/runtime.py  每条消息一次 claude CLI 调用
                   │                    ├─ 按 chat_id 选 profile（cwd）
                   │                    ├─ 按 sender open_id 选 tier（叠加 deny flags）
                   │                    ├─ --resume 续会话（per-chat 锁防交错）
                   │                    └─ audit_log.jsonl 审计
                   ├─ core/memory.py     语义记忆注入 + auto_learn（保留）
                   └─ core/scheduler.py  定时摘要/提醒/归档（保留）
                        │ subprocess, cwd = profile 目录
                        ▼
              claude CLI（唯一大脑，默认权限模式，不再 bypassPermissions）
                   ├─ profile=docs   → 工具面仅 Bash(lark-cli:*)
                   ├─ profile=thesis → Read/Edit/Write 限 cwd + lark-cli（本地毕设）
                   └─ profile=dev    → + p4 白名单（cwd = P4 工作区，按 tier 收窄）
```

## 3. 目标目录结构（全部相对路径）

```
assistant/
├── main.py                 # 改：去 wiki 预热、启动校验 env 凭证
├── config.py               # 改：全部相对路径默认值 + env 覆盖（见 §4）
├── handlers.py             # 改：走 runtime；评论流改 runtime + reply_to_comment
├── agent/
│   ├── runtime.py          # 新（~150 行）
│   └── claude.py           # 删除 → _legacy/
├── feishu/client.py        # 改：瘦身（见 §9）
├── core/                   # memory / vector_store / scheduler 不动
├── profiles/               # 新：agent 人格与权限定义
│   ├── docs/               #   CLAUDE.md + .claude/settings.json
│   ├── thesis/             #   同上（本地毕设目录助手）
│   └── dev/                #   模板 + .claude/skills/p4v/SKILL.md（部署时拷入 P4 工作区）
├── deploy/                 # 新：assistant.service / env.example / README.md
├── data/                   # 新默认数据目录（ sessions/memory/scheduler/runtime 配置/审计 ）
├── _legacy/                # 已迁移的废弃文件（context/planner/tools/system_prompt/thesis）
├── PLAN.md                 # 本文档
└── ARCHITECTURE.md         # 批准后重写为 v2
```

## 4. 路径与配置策略（相对路径优先）

原则：**代码内零硬编码绝对路径**；默认值一律相对包根；部署差异全部走环境变量。

| 配置 | 默认值（相对） | env 覆盖 |
|---|---|---|
| 数据目录 | `assistant/data/` | `ASSISTANT_DATA_DIR` |
| profile 目录 | `assistant/profiles/` | `ASSISTANT_PROFILES_DIR` |
| claude CLI | `claude`（走 PATH 查找） | `CLAUDE_EXE` |
| P4 工作区 | 无默认（未设置则 dev profile 不可用） | `P4_WORKSPACE` |
| 毕设目录 | 无默认（未设置则 thesis profile 不可用） | `THESIS_DIR` |
| 飞书凭证 | 无默认，缺失时启动报错退出 | `FEISHU_APP_ID` / `FEISHU_APP_SECRET` |

- 现有数据：已拍板**直接清理不迁移**（旧目录下仅 `assistant_sessions.json` 属于本项目，实施时删除）。
- 副作用：config.py 不再含明文密钥（原问题 3 顺带解决）。**建议随后去飞书后台轮换 App Secret。**

## 5. agent/runtime.py 设计

```python
run(chat_id, text, sender_id) -> str
  1. profile = runtime_config.chat_profiles.get(chat_id, default="docs")
  2. tier    = runtime_config.user_tiers.get(sender_id, default="read")
  3. cwd     = PROFILE_CWD[profile]（不存在则回复"该模式未配置"）
  4. prompt  = [用户信息 tier] + [记忆召回 top5] + text
  5. with per_chat_lock(chat_id):
       claude -p prompt --output-format json [--resume sid]
              --disallowedTools <TIER_DENY[tier]...>
       （cwd=profile 目录, timeout=300s, dev 900s）
  6. 持久化 session（assistant_sessions.json 沿用 20 条历史结构）
  7. audit_log.jsonl 追加 {time, chat_id, sender, profile, tier, text[:200], ok, 耗时}
```

- **每次只发新消息**（旧实现的 O(n²) 重复发送 bug 自然消除）。
- 并发：同一 chat 串行（resume 链完整性）；不同 chat 并行。
- 新增命令：「我的权限」→ 返回当前 profile + tier + 含义说明。
- 失败：超时/非零退出 → 友好文案 + stderr 摘要进审计。

`runtime_config.json`（数据目录，首次运行自动生成）：

```json
{
  "default_profile": "docs",
  "chat_profiles": { "oc_xxx": "dev" },
  "default_tier": "read",
  "user_tiers": { "ou_xxx": "submit", "ou_yyy": "edit" }
}
```

## 6. 三个 Profile

| | docs（默认） | thesis（本地） | dev（服务器） |
|---|---|---|---|
| cwd | `profiles/docs/` | `$THESIS_DIR` | `$P4_WORKSPACE` |
| 用途 | 飞书文档/表格/会议/日程/任务 CRUD | 毕设项目文件读写 | P4V 仓库联动 |
| settings allow | 仅 `Bash(lark-cli:*)` | `Read/Grep/Glob/Edit/Write` + `Bash(lark-cli:*)` | thesis 面 + p4 白名单（见 §7） |
| settings deny | `Read/Write/Edit`、`lark-cli api DELETE:*`、`lark-cli drive +delete:*`、`rm/sudo` | `rm/sudo`、`lark-cli api DELETE:*`、`drive +delete` | 同左 + `p4 revert/delete/undo/resolve`（sync 不放 deny，由 tier 控制） |
| CLAUDE.md | 文档助手人设 + lark-cli 速查表（基于已验证的 --help 输出）+ 多候选确认规则 | 毕设人设 + 卦阵手记背景 + 仅限当前目录 | 仓库背景（占位待填）+ p4 速查 + tier 语义 + submit 归因规范 |

- 非交互模式下**未 allow 的工具自动拒绝**（默认拒绝，白名单制）。
- lark-cli 用法先用手写速查表（已核对 `docs/vc/calendar/task/drive/wiki/api` 的 help）；官方 skills（`npx skills add larksuite/cli`）作为可选增强，不依赖。
- 多候选确认规则（写进 docs CLAUDE.md）：用户未指明文档时先 `docs +search`；**修改类操作多候选必须列出请用户选择**，读类可自行判断；不支持删文档（`drive +delete` 已 deny），引导用户手动。

## 7. 三层人员权限模型（read / edit / submit）

runtime 按 sender `open_id` 查 tier，**在 profile 权限天花板之上叠加 `--disallowedTools`**（deny 优先于 allow，逐层收窄）：

| tier | 能力 | 叠加 deny |
|---|---|---|
| `read`（默认） | 仓库只读分析、review 意见、文档 CRUD | `Edit/Write/NotebookEdit`、`p4 edit/add/submit/sync` + 危险集 |
| `edit` | + 工作区改代码、`p4 edit/add`（人来提交） | `p4 submit/sync` + 危险集 |
| `submit` | + `p4 sync/submit`（changelist 描述强制 `[飞书] {open_id前8}: ...` 归因） | 危险集 |

危险集 = `p4 revert/delete/undo/resolve`（所有 tier 永久禁止）；`p4 sync` 不禁止、仅 submit tier 可用（已拍板）。

四道防线：
1. harness 逐次调用叠加 deny flags（工具层硬门）；
2. CLAUDE.md 写明 tier 语义与超限拒绝话术（行为层）；
3. **P4 服务端 protections 兜底**（bot 账号无 super，只读 depot 主干、可写指定 dev 分支）——prompt 层被突破也无法越权；
4. `audit_log.jsonl` 全量审计 + P4 changelist 天然留痕。

> 诚实备注：tier 按 bot 单 P4 账号执行，服务端无法区分操作者个人；个人级归因靠 changelist 描述 + 审计日志。真·按人 P4 授权（每人自己的 ticket）列入 Phase 3 可选。

## 8. 文档评论流（改造后）

```
评论事件 → 关键词门（@助手/修改/润色…）→ runtime.run("doc_"+token, prompt, profile=docs)
         → agent 用 lark-cli 自读文档、自行完成追加/替换（+update replace_range）
         → 最终文本经 reply_to_comment 回贴到评论线程
```
harness 不再解析「追加内容：/原文→修改为」等格式约定（agent 自主执行），`replace_in_document` 删除，`reply_to_comment` 保留。

## 9. 代码变更清单

**删除（→ `_legacy/`，已完成）**：`agent/context.py`、`agent/planner.py`、`feishu/tools.py`、`system_prompt.py`、`thesis.py`；批准后追加 `agent/claude.py`。

**新增**：`agent/runtime.py`、`profiles/{docs,thesis,dev}/`、`deploy/`、`data/`、`PLAN.md`。

**修改**：
- `config.py` — 按 §4 修订（当前版本仍有绝对路径默认值）。
- `handlers.py` — sender open_id 提取；走 `runtime.run`；「我的权限」命令；评论流改造；保留已修好的调度命令拦截、reaction。
- `feishu/client.py` — 瘦身至：IM 发送（post/text）、reaction、`reply_to_comment`、scheduler 在用的 6 个 lark-cli 函数（`search_meetings/get_meeting_minute_token/get_meeting_notes/get_calendar_agenda/get_my_tasks/create_document`）、`run_lark_cli`、client 单例缓存（已修好，保留）。删除 `fetch_doc_raw/fetch_wiki_node/fetch_bitable/search_docs/fetch_doc_via_cli/update_document/replace_in_document/search_tasks` 及 wiki/docx/bitable SDK import。
- `main.py` — 去 wiki 索引预热；启动时校验 `FEISHU_APP_ID/SECRET` 缺失即报错退出。
- `core/memory.py` / `core/scheduler.py` — 不动（scheduler 的 configure 接线已修好，保留）。

## 10. 部署方案（服务器，Phase 1/2）

- Linux + Python venv + claude CLI + lark-cli + p4 CLI；`deploy/assistant.service`（systemd，EnvironmentFile 注入 env，Restart=always）。
- 飞书 WS 为出站连接，无需公网入站。
- Claude 认证：**已拍板 API 按量**（`ANTHROPIC_API_KEY` 由部署环境注入）。
- `profiles/dev/` 内容拷入 P4 工作区根目录（`.claude/` + `CLAUDE.md`），并填仓库背景。
- P4 protections 示例（deploy/README 给出完整片段）：bot 账号 `read //depot/...`、`write //depot/dev/...`、无 super。
- 已知限制：`lark-cli --as user` 单身份，「查提问人自己的任务/日程」需 per-user OAuth，列 Phase 3 可选。

## 11. Roadmap

- **Phase 0（本地）**：runtime + profiles(docs/thesis) + harness 改造 + config 相对路径化 + 数据迁移 + 冒烟验证（见 §12）。
- **Phase 1**：服务器部署，仅 docs profile，跑稳非代码场景。
- **Phase 2**：dev profile + p4v skill + P4 protections，灰度一个开发群；`runtime_config.json` 配群映射与人员名单。
- **Phase 3（已与 Phase 0 一并完成大部分）**：stream-json 进度反馈 ✅（进度消息节流原地编辑，收尾显示工具数/耗时）、定时 agent 任务 ✅（`agent_jobs` 通用机制，每周更新日志为配置项，见 scheduler.py 顶部注释）、review 流程固化 ✅（p4v skill）、per-user OAuth 评估 ✅（结论见 ARCHITECTURE.md「已知限制与评估」，暂缓）。剩余：真·按人 P4 ticket（待真实需求）。

## 12. 验证计划（Phase 0 完成标准）

1. `py_compile` 全部 .py 通过。
2. 本地冒烟（真实调 claude.exe，docs profile）：基本问答返回正常；连续两条消息验证 `--resume` 续接（同 session_id）。
3. 越权测试：docs profile 要求读本地文件 → 应被拒（Read 未 allow）；要求跑非白名单 Bash → 应被拒。验证「默认拒绝 + deny 覆盖 allow」生效。
4. 「我的权限」「查看配置」命令路径正常。
5. 清理冒烟测试产生的 session 数据。

## 13. 已落地改动（批准前状态）

| 项 | 状态 |
|---|---|
| `_legacy/` 迁移 5 文件 | 已做（可一条命令还原） |
| `config.py` 重写 | 已做，但带绝对路径默认值 → 按 §4 再修 |
| scheduler configure 接线 + `_restart_if_running` | 已做（上一轮，保留） |
| client 单例缓存 | 已做（上一轮，保留） |
| `reply_to_comment` / `replace_in_document` | 已做；后者本方案删除 |
| **当前 bot 无法启动** | handlers.py 仍 import 已移走的模块，待本方案实施后恢复 |

## 14. 开放问题（已全部拍板）

1. Claude 认证 → **API 按量**（`ANTHROPIC_API_KEY`）。
2. 旧数据 → **直接清理不留痕**，不迁移。
3. lark-cli 官方 skills → **装**（`npx skills add larksuite/cli -g`，全局，bot 与交互式 Claude 共用；deploy/README 同步服务器步骤）。
4. `p4 sync` → **不禁止，仅 submit tier 可用**。
5. 初始 `chat_profiles` / `user_tiers` → **先不填**（默认 docs + read，Phase 2 再配）。
