# 飞书云文档图片工作流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让飞书助手接收并识别原图，并安全地读取、插入和删除飞书云文档中的图片。

**Architecture:** Python 宿主负责解析飞书图片事件、下载原图并登记短期引用；独立 MCP 图片服务只返回已登记原图的 MCP image content。Claude/DeepSeek 通过 runtime 获得当前图片引用，云文档图片的读取、插入和定位继续使用 lark-cli。所有写操作都重新读取最新 block，图片删除保留用户确认门槛。

**Tech Stack:** Python 3、飞书 lark_oapi、Claude CLI stream-json、MCP stdio、lark-cli docs media commands、unittest。

**Spec:** `docs/superpowers/specs/2026-10-02-feishu-doc-image-workflow-design.md`

## Global Constraints

- 只支持飞书消息图片和飞书云文档，不支持本地 Markdown/Word 图片编辑。
- 图片 MCP 默认传递原始图片，不缩放、不压缩、不生成预览图。
- 模型只能读取宿主登记的当前请求图片，不能提供任意本地路径。
- 云文档每次写操作前必须重新读取最新 block ID。
- 删除图片必须先识别候选、取得用户明确确认，再重新读取并删除。
- admin bypass 不绕过飞书文档权限和删除确认。
- 图片二进制不写入会话历史或普通审计正文。

## Review Focus

- 飞书图片消息的 `image_key` 只登记当前消息，不能让模型借引用读取其他目录。
- 原图过大或格式不支持时必须明确失败，不能偷偷压缩成预览图。
- 图片下载失败时不应启动 Claude/DeepSeek 请求。
- 文档 block 在确认删除前发生变化时不能沿用旧 block ID。
- 普通文字消息和现有评论流不能因为图片支持而改变行为。

---

### Task 1: 图片附件解析、下载和生命周期

**Files:**
- Create: `feishu/media.py`
- Modify: `config.py`
- Modify: `handlers.py`
- Test: `tests/test_media.py`
- Test: `tests/test_handlers_media.py`

**Interfaces:**
- Produces `MediaAttachment(ref: str, path: str, media_type: str, file_name: str, source: str, message_id: str)`.
- Produces `parse_image_event(message_type: str, content: dict) -> dict | None`.
- Produces `download_message_image(client, message_id: str, image_key: str, media_root: Path) -> MediaAttachment`.
- Produces `cleanup_media(root: Path, active_refs: set[str], max_age_seconds: int) -> int`.

- [ ] **Step 1: Write failing tests**

```python
def test_parse_image_event_returns_image_key_and_type():
    assert parse_image_event("image", {"image_key": "img_v3_abc"}) == {
        "image_key": "img_v3_abc", "media_type": "image"
    }

def test_parse_text_event_returns_none():
    assert parse_image_event("text", {"text": "hello"}) is None

def test_download_rejects_missing_image_key():
    with pytest.raises(ValueError, match="image_key"):
        download_message_image(fake_client(), "om_1", "", tmp_path)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `python -m unittest tests.test_media tests.test_handlers_media -v`

Expected: FAIL because `feishu.media` and image handling do not exist.

- [ ] **Step 3: Implement the smallest media module**

Use a dataclass for `MediaAttachment`. Parse only `image_key` from the official image message content. Download the resource through a tenant-authenticated `BaseRequest` to the message resource endpoint, write bytes below `ASSISTANT_DATA_DIR/media/inbox/<message_id>/`, and reject path separators or caller-provided destinations. Preserve the response content type and choose an extension only from the response type; do not resize or recompress.

Add `ASSISTANT_MEDIA_MAX_AGE_SECONDS` with a default of `86400`. Cleanup must skip active refs and remove only files/directories below the media inbox root.

- [ ] **Step 4: Wire image events into `handlers.on_message`**

For `message_type == "image"`, parse the image key, download it before `process_message`, and pass `image_attachments=[attachment]`. If download fails, send a plain Feishu text error and return without calling `runtime.run`. Preserve existing text parsing, mention gating, group-history capture, and comment behavior.

- [ ] **Step 5: Run focused tests and full suite**

Run: `python -m unittest tests.test_media tests.test_handlers_media -v` and then `python -m unittest discover -s tests -v`.

Expected: focused tests and the existing suite pass.

- [ ] **Step 6: Commit**

```bash
git add feishu/media.py config.py handlers.py tests/test_media.py tests/test_handlers_media.py
git commit -m "feat: receive Feishu image attachments"
```

### Task 2: 受控 MCP 原图工具

**Files:**
- Create: `agent/image_mcp.py`
- Modify: `agent/runtime.py`
- Test: `tests/test_image_mcp.py`
- Test: `tests/test_runtime_images.py`

**Interfaces:**
- Produces `register_image_attachment(attachment: MediaAttachment) -> None`.
- Produces `clear_image_attachments(refs: Iterable[str]) -> None`.
- Produces MCP tool `read_image(ref: str) -> image content`.
- Extends `runtime.run(..., image_attachments: list[MediaAttachment] | None = None)`.

- [ ] **Step 1: Write failing tests**

```python
def test_image_tool_returns_original_bytes_for_registered_ref():
    attachment = make_attachment(ref="img-1", path=original_png)
    register_image_attachment(attachment)
    result = call_tool("read_image", {"ref": "img-1"})
    assert result["type"] == "image"
    assert base64.b64decode(result["data"]) == original_png.read_bytes()

def test_image_tool_rejects_unknown_ref():
    with pytest.raises(PermissionError):
        call_tool("read_image", {"ref": "not-registered"})

def test_runtime_prompt_lists_current_image_ref():
    prompt = build_prompt_for_test(image_attachments=[make_attachment(ref="img-1")])
    assert "img-1" in prompt
    assert "原图" in prompt
```

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `python -m unittest tests.test_image_mcp tests.test_runtime_images -v`

Expected: FAIL because the MCP server and runtime image arguments do not exist.

- [ ] **Step 3: Implement the allowlisted image registry**

Keep an in-process map from opaque ref to `MediaAttachment`. Validate that the resolved path is an existing regular file under the configured media inbox root. The MCP server must expose only `initialize`, `tools/list`, and `tools/call` for `read_image`; unknown methods, unknown refs, directories, and paths outside the inbox return errors. Return the original bytes and MIME type without PIL/ImageMagick processing.

- [ ] **Step 4: Attach the MCP server to Claude CLI**

Extend the per-request temporary MCP config in `_run_claude` with `assistant_image` pointing to `agent/image_mcp.py`. Register only the current request attachments before spawning Claude and clear them in a `finally` block after the process exits. Pass `image_attachments` through all runtime retry, compaction replay, and group-history retry paths.

- [ ] **Step 5: Add prompt guidance**

Inject a short trusted section naming the current refs and stating: use `assistant_image.read_image` for image understanding; the image is original; do not invent server paths; do not claim visual details without reading the image. Do not put image bytes or base64 in the text prompt or session history.

- [ ] **Step 6: Run focused tests and full suite**

Run: `python -m unittest tests.test_image_mcp tests.test_runtime_images -v` and then `python -m unittest discover -s tests -v`.

- [ ] **Step 7: Commit**

```bash
git add agent/image_mcp.py agent/runtime.py tests/test_image_mcp.py tests/test_runtime_images.py
git commit -m "feat: expose registered original images to models"
```

### Task 3: 让 docs profile 正确执行云文档图片读写

**Files:**
- Modify: `profiles/docs/CLAUDE.md`
- Modify: `profiles/docs/AGENTS.md`
- Modify: `docs/USER_GUIDE.md`
- Test: `tests/test_docs_image_guidance.py`

**Interfaces:**
- Consumes current image refs from Task 2 and existing `lark-cli` docs commands.
- Produces deterministic model guidance for `+fetch`, `+media-preview`, `+media-download`, `+media-insert`, `block_insert_after`, and `block_delete`.

- [ ] **Step 1: Write failing tests**

```python
def test_docs_profile_requires_latest_block_ids_before_image_delete():
    text = Path("profiles/docs/CLAUDE.md").read_text(encoding="utf-8")
    assert "删除图片前必须重新读取最新 block ID" in text

def test_docs_profile_requires_confirmation_for_image_delete():
    text = Path("profiles/docs/CLAUDE.md").read_text(encoding="utf-8")
    assert "明确确认" in text

def test_docs_profile_documents_original_image_reader():
    text = Path("profiles/docs/CLAUDE.md").read_text(encoding="utf-8")
    assert "assistant_image" in text
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `python -m unittest tests.test_docs_image_guidance -v`

Expected: FAIL because the current docs profile has no image workflow rules.

- [ ] **Step 3: Add the minimal docs workflow rules**

Document that current-message images must be read through `assistant_image`, cloud-document images require `docs +fetch` to find tokens and `docs +media-preview/download` to inspect them, insertion uses `docs +media-insert`, and deletion requires a candidate list plus explicit confirmation followed by a fresh fetch. State that original bytes are used and there is no automatic preview or compression.

- [ ] **Step 4: Run tests and full suite**

Run: `python -m unittest tests.test_docs_image_guidance -v` and then `python -m unittest discover -s tests -v`.

- [ ] **Step 5: Commit**

```bash
git add profiles/docs/CLAUDE.md profiles/docs/AGENTS.md docs/USER_GUIDE.md tests/test_docs_image_guidance.py
git commit -m "docs: define Feishu cloud document image workflow"
```

### Task 4: 图片插入/删除结果验证与清理验收

**Files:**
- Modify: `handlers.py`
- Modify: `agent/runtime.py`
- Modify: `docs/STATUS_AND_ROADMAP.md`
- Test: `tests/test_image_lifecycle.py`

**Interfaces:**
- Consumes `MediaAttachment` lifecycle and runtime image refs from Tasks 1–2.
- Produces cleanup after success/failure, audit metadata without image bytes, and explicit server acceptance checks.

- [ ] **Step 1: Write failing tests**

```python
def test_image_bytes_are_not_written_to_audit_payload():
    record = build_image_audit_record(make_attachment(ref="img-1"), operation="read")
    assert "data" not in record
    assert "base64" not in json.dumps(record)

def test_active_attachment_survives_until_runtime_finishes():
    assert cleanup_media(root, active_refs={"img-1"}, max_age_seconds=0) == 0

def test_failed_runtime_still_clears_attachment_registry():
    run_request_that_raises()
    assert image_registry_is_empty()
```

- [ ] **Step 2: Run tests and verify they fail**

Run: `python -m unittest tests.test_image_lifecycle -v`

Expected: FAIL because lifecycle cleanup and image-safe audit behavior are not implemented.

- [ ] **Step 3: Implement cleanup and audit fields**

Use `try/finally` around every runtime invocation that has image attachments. Audit only `ref`, `message_id`, MIME type, byte size, source, operation, and success/error status. Run stale cleanup at startup and never delete an active ref.

- [ ] **Step 4: Add server acceptance checklist**

Document the required Feishu permissions for message-resource download and docs media read/write, plus three manual checks: send a real image and ask for a description; insert that original image into a chosen cloud document; request deletion and verify the assistant asks for confirmation before deleting the selected block.

- [ ] **Step 5: Run the full suite and static checks**

Run: `python -m unittest discover -s tests -v`, `python -m compileall agent feishu handlers.py`, and `git diff --check`.

Expected: all tests pass, Python compilation succeeds, and `git diff --check` prints no errors.

- [ ] **Step 6: Commit**

```bash
git add handlers.py agent/runtime.py docs/STATUS_AND_ROADMAP.md tests/test_image_lifecycle.py
git commit -m "feat: finish Feishu document image lifecycle"
```

## Spec Coverage Self-Review

- Image message reception and controlled temporary storage: Task 1.
- Original image MCP transport and model prompt integration: Task 2.
- Cloud document image read/insert/delete workflow: Task 3.
- Permission, audit, cleanup, and manual acceptance: Task 4.
- The plan intentionally does not add local document support or image generation, matching the spec.
