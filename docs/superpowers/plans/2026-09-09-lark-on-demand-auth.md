# Lark On-Demand User Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep lark-cli bot-first and send a Feishu authorization link only when a request genuinely requires user identity and no reusable user login exists.

**Architecture:** A deterministic preflight classifies personal-resource requests before Claude starts. A focused authorization manager checks the fixed `assistant-bot` profile, initiates lark-cli Device Flow, sends only the verification URL through the existing Feishu client, waits for completion, and then allows the original request to run once. Claude profile permissions prohibit direct authentication commands.

**Tech Stack:** Python 3.13 standard library, `subprocess`, `threading`, `unittest`, lark-cli Device Flow, existing lark-oapi client.

## Global Constraints

- Default identity is bot; user identity is selected only for explicit personal calendar, task, meeting/minutes, mail, attendance, or contact requests.
- Windows and Linux both send the verification URL into Feishu and never open a browser automatically.
- Every lark-cli command uses `--profile assistant-bot` (or configured `LARK_CLI_PROFILE`).
- Device code, access token, refresh token, App Secret, and API keys never appear in Feishu messages or audit logs.
- One authorization flow may run at a time; concurrent callers reuse its result.
- Authorization success retries the original user request once; rejection, expiry, or failure never loops.
- lark-cli persists and refreshes OAuth tokens; Python stores none.

---

### Task 1: Safe lark-cli profile bootstrap

**Files:**
- Create: `feishu/lark_profile.py`
- Modify: `main.py`
- Test: `tests/test_lark_profile.py`

**Interfaces:**
- Produces: `sync_lark_profile(env: Mapping[str, str], run: Callable[..., CompletedProcess]) -> None`
- Consumes: `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, and `LARK_CLI_PROFILE` from process environment.

- [ ] **Step 1: Write failing tests**

```python
def test_sync_uses_stdin_for_secret_and_never_argv():
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")
    sync_lark_profile({
        "FEISHU_APP_ID": "cli_test",
        "FEISHU_APP_SECRET": "secret-value",
        "LARK_CLI_PROFILE": "assistant-bot",
    }, fake_run)
    argv, kwargs = calls[0]
    assert "secret-value" not in argv
    assert kwargs["input"] == "secret-value\n"
    assert argv == ["lark-cli", "config", "init", "--name", "assistant-bot",
                    "--app-id", "cli_test", "--app-secret-stdin", "--brand", "feishu"]
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m unittest tests.test_lark_profile -v`
Expected: import failure because `feishu.lark_profile` does not exist.

- [ ] **Step 3: Implement the synchronizer**

```python
def sync_lark_profile(env=os.environ, run=subprocess.run):
    app_id = env.get("FEISHU_APP_ID", "")
    secret = env.get("FEISHU_APP_SECRET", "")
    profile = env.get("LARK_CLI_PROFILE", "assistant-bot")
    if not app_id or not secret:
        raise RuntimeError("missing Feishu credentials")
    result = run(
        ["lark-cli", "config", "init", "--name", profile,
         "--app-id", app_id, "--app-secret-stdin", "--brand", "feishu"],
        input=secret + "\n", text=True, capture_output=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "lark-cli config failed")[:300])
```

Call it during startup before the scheduler and WebSocket client are started. Log success/failure without printing credentials.

- [ ] **Step 4: Run tests and startup import checks**

Run: `python -m unittest tests.test_lark_profile -v && python -m compileall -q .`
Expected: all tests pass, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add feishu/lark_profile.py main.py tests/test_lark_profile.py
git commit -m "feat: safely sync lark cli profile"
```

### Task 2: Bot-first request classification

**Files:**
- Create: `feishu/identity.py`
- Test: `tests/test_identity.py`

**Interfaces:**
- Produces: `required_user_domain(text: str) -> str | None`
- Domain values: `calendar`, `task`, `vc`, `minutes`, `mail`, `attendance`, `contact`.

- [ ] **Step 1: Write failing table-driven tests**

```python
class IdentityTests(unittest.TestCase):
    def test_personal_resources_require_user(self):
        cases = {
            "看看我明天的日程": "calendar",
            "我的未完成任务": "task",
            "查我昨天参加的会议": "vc",
            "整理我的会议纪要": "minutes",
            "看看我的邮件": "mail",
            "我的考勤记录": "attendance",
            "查看我的个人信息": "contact",
        }
        for text, domain in cases.items():
            self.assertEqual(required_user_domain(text), domain)

    def test_shared_resources_stay_bot_first(self):
        for text in ["读取项目周报", "搜索知识库", "列出群聊", "创建一篇文档"]:
            self.assertIsNone(required_user_domain(text))
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m unittest tests.test_identity -v`
Expected: import failure because `feishu.identity` does not exist.

- [ ] **Step 3: Implement minimal ordered phrase matching**

Implement normalized Chinese matching with explicit personal markers (`我`, `我的`, `本人`) plus domain nouns. Do not classify generic shared resources as user requests.

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_identity -v`
Expected: all cases pass.

- [ ] **Step 5: Commit**

```bash
git add feishu/identity.py tests/test_identity.py
git commit -m "feat: classify requests requiring user identity"
```

### Task 3: Single-flight Device Flow manager

**Files:**
- Create: `feishu/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Produces: `AuthResult(ok: bool, message: str)` dataclass.
- Produces: `LarkAuthManager.ensure_user(domain: str, notify: Callable[[str], None]) -> AuthResult`.
- Constructor accepts profile, command runner, timeout, and clock dependencies.

- [ ] **Step 1: Write failing tests for status, link redaction, completion, and concurrency**

```python
def test_existing_user_login_skips_device_flow():
    runner = FakeRunner(status={"identity": "user"})
    result = LarkAuthManager("assistant-bot", runner).ensure_user("calendar", lambda _: None)
    assert result.ok
    assert runner.login_calls == 0

def test_missing_login_sends_only_verification_url():
    runner = FakeRunner(
        status={"identity": "bot", "note": "No user logged in"},
        start={"verification_uri_complete": "https://example.test/verify?code=public",
               "device_code": "private-device-code"},
        finish={"ok": True},
    )
    messages = []
    result = LarkAuthManager("assistant-bot", runner).ensure_user("calendar", messages.append)
    assert result.ok
    assert messages == ["此操作需要你的飞书用户授权，请点击链接完成授权：\nhttps://example.test/verify?code=public"]
    assert "private-device-code" not in "".join(messages)
```

Add a two-thread test asserting only one `--no-wait` command runs and both callers receive the same final result.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_auth -v`
Expected: import failure because `feishu.auth` does not exist.

- [ ] **Step 3: Implement auth status and JSON parsing**

Use `lark-cli --profile <profile> auth status` and parse its JSON output. Treat `identity == "user"` as reusable login. Keep stderr and raw output out of user-facing messages when they may contain device credentials.

- [ ] **Step 4: Implement Device Flow**

Start with:

```text
lark-cli --profile <profile> auth login --no-wait --json --domain <domain>
```

Extract the verification URL and private device code. Notify with URL only. Complete with:

```text
lark-cli --profile <profile> auth login --json --device-code <private-code>
```

Run completion with a bounded timeout. Return a stable Chinese error for invalid secret, rejection, expiry, timeout, or malformed output.

- [ ] **Step 5: Implement single-flight locking**

Use one `Condition` and one in-flight record. A leader starts/completes authorization; followers wait for and reuse the result. Clear state after completion so a later expired login may trigger a new flow.

- [ ] **Step 6: Run tests**

Run: `python -m unittest tests.test_auth -v`
Expected: all auth behavior and concurrency tests pass.

- [ ] **Step 7: Commit**

```bash
git add feishu/auth.py tests/test_auth.py
git commit -m "feat: add single flight lark authorization"
```

### Task 4: Integrate preflight with message handling

**Files:**
- Modify: `handlers.py`
- Test: `tests/test_handlers_auth.py`

**Interfaces:**
- Consumes: `required_user_domain(text)` and global `auth_manager.ensure_user(domain, notify)`.
- Uses existing `send_message(client, chat_id, text)` for the authorization URL.

- [ ] **Step 1: Write failing handler tests**

```python
def test_bot_first_message_does_not_preflight_auth():
    response = process_message("搜索项目周报", "chat", "user", client)
    auth_manager.ensure_user.assert_not_called()

def test_personal_request_authorizes_before_runtime():
    auth_manager.ensure_user.return_value = AuthResult(True, "")
    process_message("查看我的日程", "chat", "user", client)
    auth_manager.ensure_user.assert_called_once()
    runtime.run.assert_called_once()

def test_failed_authorization_does_not_run_claude():
    auth_manager.ensure_user.return_value = AuthResult(False, "授权已过期")
    result = process_message("查看我的日程", "chat", "user", client)
    assert result == "授权已过期"
    runtime.run.assert_not_called()
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m unittest tests.test_handlers_auth -v`
Expected: personal request does not call the missing preflight.

- [ ] **Step 3: Add preflight before runtime.run**

Classify the message after deterministic scheduler/permission commands and before progress setup. For personal requests, call `ensure_user`; the notify callback sends the link to the same `chat_id`. On success, call `runtime.run` exactly once; on failure return the stable auth message.

- [ ] **Step 4: Run handler and full tests**

Run: `python -m unittest discover -s tests -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add handlers.py tests/test_handlers_auth.py
git commit -m "feat: authorize personal requests before agent runtime"
```

### Task 5: Enforce bot-first Claude behavior

**Files:**
- Modify: `profiles/docs/CLAUDE.md`
- Modify: `profiles/docs/.claude/settings.json`
- Modify: `ARCHITECTURE.md`
- Modify: `ARCHITECTURE_DIAGRAM.md`
- Test: `tests/test_profile_policy.py`

**Interfaces:**
- Claude may execute ordinary `lark-cli --profile assistant-bot ... --as bot|user` business commands.
- Claude may not execute `auth login`, `auth logout`, or device-code commands.

- [ ] **Step 1: Write failing policy tests**

```python
def test_docs_profile_is_bot_first_and_denies_auth_commands():
    instructions = Path("profiles/docs/CLAUDE.md").read_text(encoding="utf-8")
    settings = json.loads(Path("profiles/docs/.claude/settings.json").read_text(encoding="utf-8"))
    assert "默认使用 `--as bot`" in instructions
    denies = settings["permissions"]["deny"]
    assert "Bash(lark-cli * auth login:*)" in denies
    assert "Bash(lark-cli * auth logout:*)" in denies
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m unittest tests.test_profile_policy -v`
Expected: bot-first wording and deny entries are absent.

- [ ] **Step 3: Update profile policy and diagrams**

State that shared resources default to bot and personal resources use user only after Python preflight. Add deny patterns for authentication mutation commands with and without an explicit profile. Update architecture docs to show the preflight authorization manager and URL callback.

- [ ] **Step 4: Run full tests and JSON validation**

Run: `python -m unittest discover -s tests -v && python -m json.tool profiles/docs/.claude/settings.json > $null && python -m compileall -q .`
Expected: all tests pass and commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add profiles/docs/CLAUDE.md profiles/docs/.claude/settings.json ARCHITECTURE.md ARCHITECTURE_DIAGRAM.md tests/test_profile_policy.py
git commit -m "docs: enforce bot first lark identity policy"
```

### Task 6: End-to-end local verification

**Files:**
- Modify: `deploy/README.md`
- Modify: `handoff-20260909-025106.md` only if the historical handoff is explicitly marked superseded; otherwise leave it unchanged.

**Interfaces:**
- Uses the permanent Windows `FEISHU_APP_ID` and `FEISHU_APP_SECRET` without printing either value.

- [ ] **Step 1: Sync the real lark-cli profile safely**

Run a PowerShell pipeline that retrieves the User-scope environment values and sends the Secret through stdin to `lark-cli config init --name assistant-bot --app-id <id> --app-secret-stdin --brand feishu`. Do not interpolate or print the Secret.

- [ ] **Step 2: Verify profile and initiate a disposable authorization link**

Run: `lark-cli --profile assistant-bot auth status`
Expected: valid profile JSON, without `client secret is invalid`.

Run: `lark-cli --profile assistant-bot auth login --no-wait --json --domain calendar`
Expected: JSON containing a verification URL. Redact the device code from recorded output and allow the disposable request to expire if not used.

- [ ] **Step 3: Run all automated checks**

Run: `python -m unittest discover -s tests -v`
Expected: zero failures and zero errors.

Run: `python -m compileall -q .`
Expected: exit code 0.

Run: `git status --short`
Expected: clean after final commit.

- [ ] **Step 4: Document operator behavior**

Add to `deploy/README.md`: profile bootstrap, bot-first behavior, user-resource authorization-link flow, expected expiry behavior, and troubleshooting for invalid App Secret and 429 model-provider rate limits.

- [ ] **Step 5: Commit**

```bash
git add deploy/README.md
git commit -m "docs: explain on demand lark authorization"
```
