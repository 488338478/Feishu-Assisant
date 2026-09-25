# Context Compaction Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect sessions that are approaching the configured context budget, ask the user before compacting, and resume the original request after a confirmed compaction.

**Architecture:** The runtime keeps a small pending-compaction record per conversation scope. A preflight estimate gates the current request; the handler presents a confirmation message, and a later confirmation resumes the saved request. Compaction runs through a separate `claude -p --resume <session> "/compact"` call, preserving the old session if that call fails.

**Tech Stack:** Python standard library, existing Claude Code CLI, `unittest`, JSON session persistence.

**Spec:** The approved design in the conversation: confirmation is required immediately before compaction; cancellation must not mutate the session; successful compaction replays the original request once.

## Global Constraints

- Never compact without an explicit user confirmation.
- Never discard the existing session before compaction succeeds.
- Keep pending requests isolated by the existing `chat + chat_type + profile` conversation key.
- Preserve the existing manual 「清除上下文」 behavior.
- Do not add a third-party dependency.

---

### Task 1: Add context budget estimation and confirmation state

**Files:**
- Modify: `config.py`
- Modify: `agent/runtime.py`
- Test: `tests/test_context_compaction.py`

**Interfaces:**
- `runtime.context_compaction_status(conversation_scope_key) -> dict`
- `runtime.request_compaction(...) -> dict`
- `runtime.confirm_compaction(...) -> tuple[str, dict]`

- [x] **Step 1: Write failing tests** for a request being paused at the threshold, cancellation clearing the pending request, and confirmation replaying exactly once.
- [x] **Step 2: Run `python -m unittest tests.test_context_compaction -v` and verify the new tests fail because the state and APIs do not exist.
- [x] **Step 3: Implement JSON-backed pending state, configurable threshold, and deterministic character-based estimate with a clear estimate marker in the confirmation text.
- [x] **Step 4: Run the focused tests and verify they pass.

### Task 2: Execute Claude Code compaction safely

**Files:**
- Modify: `agent/runtime.py`
- Test: `tests/test_context_compaction.py`

**Interfaces:**
- `_compact_session(session_id, cwd, deny, timeout, model) -> tuple[str | None, int]`

- [x] **Step 1: Add a failing test that verifies compaction calls the resumed CLI with `/compact`, keeps the original session on failure, and returns a new session ID when the CLI reports one.
- [x] **Step 2: Run the focused test and verify the expected failure.
- [x] **Step 3: Implement the minimal stream-json parser and safe failure handling.
- [x] **Step 4: Run focused runtime tests, then the full test suite.

### Task 3: Route confirmation messages through the Feishu handler

**Files:**
- Modify: `handlers.py`
- Test: `tests/test_context_compaction.py`

**Interfaces:**
- Explicit confirmation phrases: `确认压缩`, `压缩`, `继续压缩`
- Explicit cancellation phrases: `取消压缩`, `暂不压缩`, `不要压缩`

- [x] **Step 1: Add failing handler tests for confirmation, cancellation, and ordinary messages while a confirmation is pending.
- [x] **Step 2: Run the focused handler tests and verify they fail.
- [x] **Step 3: Add deterministic command handling before normal model routing; preserve the original user message when confirming.
- [x] **Step 4: Run all tests with `python -m unittest discover -s tests -v`.

### Task 4: Document configuration and operational behavior

**Files:**
- Modify: `README.md`
- Modify: `docs/USER_GUIDE.md`

- [x] **Step 1: Document the threshold environment variable, confirmation messages, and failure fallback.
- [x] **Step 2: Run the full test suite and inspect the final diff.
