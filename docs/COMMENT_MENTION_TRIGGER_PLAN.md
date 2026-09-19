# 飞书文档评论 @机器人 触发 —— 修复计划

## 2026-09-12 执行修订（以下内容优先于历史计划）

用户已确认开始实施，并新增：回复机器人后无需再次 @，同一评论线程延续上下文。

**目标**：未解决的局部评论首次 @机器人后执行指令并原线程回复；后续紧接机器人回复的追问无需 @。

**接口事实**：官方 `Notice` 的 file_token/file_type/from_user_id/to_user_id/from_user_type/notice_type 均位于 event.notice_meta；comment_id/reply_id/is_mentioned 位于 event；event_id 位于 header。事件没有正文，也没有独立的父 reply_id 字段。API 返回的回复按线程排列，因此追问定义为：接收者是机器人、add_reply、触发回复前一条是机器人、没有明确 @其他人。不能声称可以区分 UI 中任意跨层回复关系。

**架构**：WS 回调只做候选筛选并转后台。后台按完整文档 token 串行，按文档类型+token+comment_id 续接 runtime；确定性分页回读到确切 reply_id，注入之前的线程文本和引用。飞书正文读写交给 agent，投递由 Python 统一负责。bot 身份回读和回复，不自动发起 user 授权。

**边界**：全文评论、已解决评论在执行前跳过并记原因；不自动恢复评论，不向别处发消息。缺字段或身份不匹配拒绝执行。只有结构化 person 才算 mention，关键词没有触发作用。

**实施步骤**（沿用 unittest / mock，保留当前工作区的并行改动）：

- [x] `feishu/doc_comment.py`：通知解析、候选筛选、精确回复门控、线程 prompt；测试真实层级、身份、追问、乱序、不串评论。
- [x] `feishu/client.py`：调用时解析 launcher，完整 JSON 信封解析、非零退出失败；评论卡片读取、回复分页、+add-reply。测试 argv、分页与错误。
- [x] `feishu/comment_service.py` + `comment_store.py`：后台调度、文档锁、SQLite 业务去重及结果保存。执行失败状态不自动重跑写操作；结果已保存后只重试投递，超时投递先核对已有机器人回复。进程重启后保留去重；推理中崩溃标为结果不确定，需人工处理。
- [x] `handlers.py`、config、env：小范围接线；保持 IM 并行改动。同步 docs profile 双文件、使用说明和架构。
- [x] 观察新增用例失败后实现，通过评论测试及全量回归；低层 CLI mock 验证实际 runtime 的同线程续接与异线程隔离。
- [x] 飞书实机回读确认：用户发出的新评论 @和原线程无 @追问均执行并回贴；追问答案明确引用原线程要求。详见 [实测记录](COMMENT_MENTION_E2E_REPORT.md)。
- [ ] 独立新增测试评论验证随机代号跨轮记忆、无 @新线程拒绝、多人插话及重投恢复；已有离线测试不能替代这些实机边界验收。

**验证命令**（assistant 目录）：`python -m unittest discover -s tests -p test_doc_comment.py -v`，`python -m unittest discover -s tests -v`。

---

以下为历史计划，错误字段层级、宽松门控、交给 agent 猜测触发回复、按文档共享会话等设计均已由上述修订替代。

> 2026-09-11 定稿。背景：用户想要「飞书文档评论里 @ 机器人就调用机器人」；核查发现功能已有半成品接线但从未真正可用，本计划把它修好。

## 现状核查结论

- 事件 `drive.notice.comment_add_v1` 已注册（`main.py:61`），handler `on_doc_comment` 存在（`handlers.py:154-189`）。
- 但触发条件是**关键词子串匹配**（`@助手/修改/润色/帮我看` 等，`handlers.py:168-170`），不是真正的 @ 检测；且正文提取只认 `text_run` 元素（`handlers.py:161-165`），**飞书真正的 @ 是 `person` 元素，被直接丢掉** —— 纯 @ 评论提取出空文本后 early-return。
- 根本问题：该事件的真实 payload 很可能是**通知形态**（`notice_meta.{file_token,file_type,notice_type}`、`from_user_id`、`to_user_id`、`is_mentioned`，**不含评论正文**），现有代码假设的是评论对象形态（`comment.content`）。形态不对 → 永远拿不到正文和提及。
- 附加缺陷：`sender_id=""`（无身份归属）、评论处理**同步阻塞 WS 事件循环**（最长 300s 冻结整个 bot）、回复通道 `run_lark_cli` 用裸 `lark-cli`（Windows 上必挂，已知 P1）、`file_type` 硬编码 `docx`、无重复投递去重、无任何测试覆盖。

**已确认的决策**：触发条件改为**只认 @机器人**（删除关键词触发）；并发加固**只修评论路径**（线程化 + 去重），其余竞态仅在文档记录。

## 并发问题结论（核查附带产出）

- **能并发**：每条 IM 消息一个 daemon 线程（`handlers.py:152`）+ 每次调用一个全新 `claude` 子进程（`runtime.py:174` `subprocess.Popen`，无进程复用）。不同群/不同人并行，无上限；同一群由 per-chat 锁串行（`runtime.py:278`，保护 `--resume` 会话链）。
- **两个已知坑**（本次只修评论相关的第一个）：
  1. `on_doc_comment` 不走线程，在 SDK 单事件循环线程上同步跑 `runtime.run` → **一条评论冻住整个 bot 最长 300s**（心跳停、ack 延迟、飞书重试投递造成重复处理）。← 本次修复
  2. `core/memory.py` / `runtime._save_sessions` 无锁整文件重写，多人并发可能损坏 JSON（`docs/STATUS_AND_ROADMAP.md:81-82` 已登记）→ **本次不动，只保证文档里有记录**。

## 修复设计

### 1. 新增 `feishu/doc_comment.py`（纯函数模块，零 I/O，风格对齐 `message_gate.py`）

- `DocComment` frozen dataclass：`doc_token / file_type / comment_id / reply_id / notice_type / event_id / author_id / text / text_known / mention_ids / is_mentioned / to_ids`；`dedup_key` 属性按 `event_id → comment_id:reply_id → comment_id` 取优先级。
- `parse_comment_event(payload) -> DocComment`：**防御式兼容两种形态**
  - 通知形态：`notice_meta.file_token/file_type`、`comment_id`、`reply_id`、`from_user_id.{open_id}`、`to_user_id.{open_id}`、`is_mentioned`、`event_id`；`text_known=False`（正文不在事件里，交给 agent 回读）。
  - 评论对象形态：`comment.content[].elements[]` 走 `_walk_elements`（`text_run/text` 累加文本；`person/mention/mention_user/at` 收集 id 不产生文本；`comment.user_id` 为作者；`object.obj_token/obj_type`）；`content` 缺失时读 `reply_list.replies[-1]`。
  - `_ids_of`：从字符串或 `{open_id,user_id,union_id}` dict 取**全部** id（应对 mention 元素给 user_id 而非 open_id 的类型不确定问题）。
  - 任何畸形 payload（`None`/非 dict/缺字段）不抛异常，返回空字段 DocComment。
- `doc_comment_gate(comment, bot_open_id, extra_bot_ids=()) -> str`：fail closed（无 bot id → 拒绝）；机器人自己的评论（`author_id` 命中）→ 拒绝防自回环；命中规则返回规则名（`mention-id` / `to-user` / `is-mentioned`），否则 `""`。`is-mentioned` 布尔位仅在**没有任何 id 可核对**时才采信。
- `CommentDedup(max_size=500, ttl=86400)`：有界去重（超限按插入序淘汰、过期清理），`seen(key, now=None)` 支持测试注入时间。

### 2. 重写 `handlers.on_doc_comment`（`handlers.py:154-189`）

- 同步部分（WS 线程内，微秒级）：打印原始 payload（截 800 字符）+ 结构化摘要 `gate=... mentions=... to=... author=...` → `parse_comment_event` → 门控（`BOT_OPEN_ID` + 新增 `config.BOT_IDS`）→ 校验 `doc_token/comment_id` → `_DOC_COMMENT_DEDUP.seen()`（**同步登记**防重复事件竞态）→ 起 `threading.Thread(target=_handle_doc_comment, daemon=True)`（与 `on_message` 一致）。
- 工作线程 `_handle_doc_comment(comment)`：
  - `text_known=True 且 text 为空`（只 @ 无指令）→ **不烧推理**，直接回复「请补充具体指令」。
  - 其余 → `runtime.run("doc_" + comment.doc_token, prompt, sender_id=comment.author_id, profile="docs")`。**session key 用完整 token**（消除现有 `doc_token[:8]` 碰撞）；`sender_id` 用评论人 id（tier + 审计归因生效）。
  - 回复 `reply_to_comment(doc_token, comment_id, text[:2000], comment.file_type)`；失败只打日志不抛。
  - prompt：事件带正文则直接嵌入；`text_known=False` 时明确指示 agent 用 `lark-cli drive +batch-query-comments --token ... --type ... --comment-ids ...` 回读正文（读不到退回 `+list-comments`）。要求回复不要 @ 任何人。
- 模块级新增可 patch 常量：`BOT_IDS`（来自 config）、`_DOC_COMMENT_DEDUP = CommentDedup()`、`_DOC_REPLY_MAX = 2000`。

### 3. `feishu/client.py` 两处修复

- `run_lark_cli`（`client.py:24`）：裸 `"lark-cli"` → `resolve_lark_cli()`（`feishu/lark_command.py`，与 `auth.py:29`、`lark_profile.py:23` 一致），**在函数内调用**（每次读 PATH，可测试 patch）。关掉 Windows P1。
- `reply_to_comment(doc_token, comment_id, text, file_type="docx")`（`client.py:208-216`）：裸 `api POST .../replies` → **`lark-cli drive +add-reply --token --type --comment-id --content`**（`--content` 为 `[{"type":"text","text":...}]` JSON）。理由：自动解包 wiki token、统一支持 sheet/file/slides 等类型、预检全文评论/已解决评论并给出可读错误、与 1.0.94 迁移方向一致；可测试性相同（断言 argv）。旧实现作为注释保留作回退。

### 4. `config.py` + `deploy/env.example`

- `config.py` 新增 `BOT_IDS`（env `ASSISTANT_BOT_IDS`，逗号分隔，去空）：mention 元素若给 user_id/union_id 而非 open_id 时的逃生口；实机日志里看到机器人 id 后填入即可。

### 5. `main.py` 不改

注册已存在；不注册 `drive.notice.comment_reply_v1`（官方订阅接口仅支持 `comment_add_v1` 一种，且该事件名覆盖「添加评论、回复」两种 notice_type，回复以 `notice_type=add_reply` 区分）→ `StartupOrderTests` 保持绿。

## 文件改动清单

| 文件 | 动作 |
|---|---|
| `feishu/doc_comment.py` | 新增（纯函数解析+门控+去重，中文注释/type hints） |
| `handlers.py` | 重写 `on_doc_comment`；新增 `_handle_doc_comment`、`_doc_comment_prompt`、模块级常量 |
| `feishu/client.py` | `run_lark_cli` 接 `resolve_lark_cli()`；`reply_to_comment` 改 `drive +add-reply` + `file_type` 参数 |
| `config.py` | 新增 `BOT_IDS` |
| `tests/test_doc_comment.py` | 新增（约 28 用例，见下） |
| `tests/test_lark_client.py` | +2 用例（launcher 解析、`+add-reply` argv） |
| `deploy/env.example` | +1 行 `ASSISTANT_BOT_IDS` 说明 |
| `docs/STATUS_AND_ROADMAP.md` | 更新「文档评论」行、launcher P1 已修 |
| `docs/USER_GUIDE.md` | 评论用法、事件清单、FAQ「@ 了没反应」排查 |
| `ARCHITECTURE.md` | 重写「文档评论流」段 |
| `profiles/docs/CLAUDE.md` + `AGENTS.md` | 同步补 `+batch-query-comments` / `+add-reply` 速查与「回复不 @ 人」 |
| `README.md` | 文档入口表加本计划一行（可选） |

**提交顺序**（每步可独立跑测试）：① launcher 修复 → ② 新增 `doc_comment.py` + 纯函数测试 → ③ `reply_to_comment` 改造 → ④ `handlers.py` 重写 + config（唯一行为变更点）→ ⑤ 文档同步。

## 测试（stdlib unittest + mock，沿用现有 idiom）

- 解析：person 元素提取 id 不产文本、多块拼接、两种形态解析、reply_list 兜底、字符串 content、畸形 payload 不抛、`@_user_N` 占位符清洗、dedup_key 优先级。
- 门控：mention id 命中/他人 mention 拒绝/无 bot id fail closed/机器人自评拒绝/`to-user` 命中/`is-mentioned` 兜底（有他人 mention id 时布尔位不采信）/`extra_bot_ids` 命中/`{"user_id":...}` 部分 id 命中。
- 去重：同 key 只过一次、超 max_size 淘汰最旧、过期清理（注入 now）。
- handler 接线（`_SyncThread` 照抄 `tests/test_lark_profile.py:116-121` FakeThread 写法；每用例 patch `handlers._DOC_COMMENT_DEDUP` 隔离）：未 @ 无副作用（`runtime.run`/`reply_to_comment`/`Thread` 均未调用，对齐 `test_message_gate.py:62-71`）、@ 后 `runtime.run` 收到 `sender_id=ou_author`、重复事件只处理一次、纯 @ 只回复不推理、缺 doc_token 忽略、`text_known=False` 时 prompt 含 `batch-query-comments`、回复失败只记日志。
- `test_lark_client.py`：launcher 被解析（且是调用时解析）、`+add-reply` argv 形状。
- `StartupOrderTests` 无需改动（main.py 不变）。

## 验证

1. `python -m unittest discover -s tests -v`（在本目录）全绿；测试数约 46 → 76。
2. **实机（Windows）**：
   ```powershell
   $env:FEISHU_APP_ID='<app id>'; $env:FEISHU_APP_SECRET='<secret>'
   python -m assistant.main
   ```
   在 docx 评论里用飞书 @ 选择机器人写指令 → 日志依次出现 `[DOC] {raw}` → `[DOC] accepted gate=mention-id ...` → `[RUNTIME]` → 评论区收到回复；无 @ 的评论无任何副作用；机器人自己的回复不产生回环。
3. **飞书开放平台前置（代码查不了，需人工）**：事件订阅方式为长连接、已订阅 `drive.notice.comment_add_v1`、开通评论读/写 + 文档读权限、**改权限后重新发布版本**、机器人是目标文档协作者。若日志 `[DOC] ignored: gate=reject mentions=(...)` 显示机器人 id 与 open_id 不同型 → 填入 `ASSISTANT_BOT_IDS` 重启。

## 风险与兜底

- payload 若为第三种形态：防御式解析 + `is-mentioned` 兜底 + 全量原始日志，一次实机即可补齐映射。
- 旧版 lark-cli 无 `+add-reply`：失败打日志，注释里保留裸 API POST 回退写法。
- 评论风暴线程无上限：与 IM 同等水平；去重 + per-doc 锁限制实际并发（「有限并发队列」已在 STATUS 文档登记为独立缺口，本次不做）。
- 注意 `.worktrees/operations-console`（分支 codex/operations-console）为并行工作流，本次改动全部落在 master 主工作区。
