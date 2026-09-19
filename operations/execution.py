"""Observable execution of canonical policy decisions, without a shell."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .policy import MAX_INPUT, check_tool
from .store import OpsStore, _redact

MAX_OUTPUT = 256_000
LOCK_ROOT = Path(tempfile.gettempdir()) / 'assistant-operations-locks'


def _lock_path(workspace):
    key = os.path.normcase(str(Path(workspace).resolve()))
    return LOCK_ROOT / (hashlib.sha256(key.encode()).hexdigest() + '.lock')


def _acquire(file):
    file.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(file):
    file.seek(0)
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


@contextmanager
def workspace_lock(workspace, *, timeout=30, cancelled=lambda: False):
    """Hold across the whole request; pass the yielded lease in trusted context.

    OS byte locks serialize independent Python processes and release on crashes.
    The lease is kept in protected runtime context, never tool input. Children
    verify the random owner token AND that another process still holds the lock.
    """
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    path = _lock_path(workspace)
    file = path.open('a+b')
    acquired = False
    try:
        start = time.monotonic()
        while True:
            if cancelled():
                raise RuntimeError('run cancelled while waiting for workspace lock')
            try:
                _acquire(file)
                acquired = True
                break
            except OSError:
                if time.monotonic() - start >= timeout:
                    raise TimeoutError('workspace is busy')
                time.sleep(.025)
        token = uuid.uuid4().hex
        file.seek(1)
        file.truncate()
        file.write(token.encode('ascii'))
        file.flush()
        yield {'token': token}
    finally:
        if acquired:
            _release(file)
        file.close()


def validate_workspace_lease(workspace, lease):
    if not isinstance(lease, dict) or not isinstance(lease.get('token'), str):
        return False
    try:
        with _lock_path(workspace).open('r+b') as file:
            file.seek(1)
            if file.read(100).decode('ascii') != lease['token']:
                return False
            try:
                _acquire(file)
            except OSError:
                return True
            _release(file)
            return False
    except (OSError, UnicodeError):
        return False


def resolve_executable(name):
    """Never execute npm .cmd/.ps1 shims; use the installed native CLI."""
    found = shutil.which(name)
    if name == 'lark-cli' and os.name == 'nt':
        roots = [Path(found).parent] if found else []
        if os.environ.get('APPDATA'):
            roots.append(Path(os.environ['APPDATA']) / 'npm')
        for root in roots:
            native = root / 'node_modules' / '@larksuite' / 'cli' / 'bin' / 'lark-cli.exe'
            if native.is_file():
                return str(native)
    if not found or (os.name == 'nt' and Path(found).suffix.lower() != '.exe'):
        raise RuntimeError(f'native executable unavailable: {name}')
    return found


def kill_process_tree(process):
    if os.name == 'nt':
        systemroot = os.environ.get('SystemRoot', r'C:\Windows')
        subprocess.run([str(Path(systemroot)/'System32'/'taskkill.exe'), '/PID', str(process.pid), '/T', '/F'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def run_process(argv, *, cwd, timeout=60, cancelled=lambda: False):
    executable = resolve_executable(argv[0])
    env = os.environ.copy()
    # P4CONFIG can select another client or contain execution-affecting options.
    for key in ('P4CONFIG', 'P4ENVIRO', 'P4DIFF', 'P4EDITOR', 'P4MERGE'):
        env.pop(key, None)
    env['P4CONFIG'] = '__assistant_disabled_p4config__'
    env['P4ENVIRO'] = os.devnull
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    process = subprocess.Popen([executable, *argv[1:]], cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, **kwargs)
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(stream, buffer):
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            remaining = MAX_OUTPUT - len(buffer)
            buffer.extend(chunk[:max(0, remaining)])
            if len(chunk) > remaining:
                overflow.set()

    readers = [threading.Thread(target=drain, args=(stream, buf), daemon=True) for stream, buf in zip((process.stdout, process.stderr), buffers)]
    for reader in readers:
        reader.start()
    started = time.monotonic()
    try:
        while process.poll() is None:
            if cancelled():
                raise RuntimeError('run cancelled; process terminated, effect may have partially occurred')
            if overflow.is_set():
                raise RuntimeError('tool output limit exceeded; effect may have partially occurred')
            if time.monotonic() - started > timeout:
                raise TimeoutError('tool timed out; effect may have partially occurred')
            time.sleep(.025)
        for reader in readers:
            reader.join(timeout=2)
        if overflow.is_set() or any(reader.is_alive() for reader in readers):
            raise RuntimeError('tool output limit or pipe deadline exceeded')
        return process.returncode, *(bytes(buf).decode('utf-8', errors='replace') for buf in buffers)
    finally:
        if process.poll() is None:
            try:
                kill_process_tree(process)
            finally:
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            stream.close()


def business_success(value):
    if isinstance(value, dict):
        if value.get('ok') is False or value.get('success') is False:
            return False
        if 'code' in value and value['code'] not in (0, '0', None):
            return False
        if value.get('result') in ('failed', 'partial_success'):
            return False
        return all(business_success(v) for v in value.values())
    if isinstance(value, list):
        return all(business_success(v) for v in value)
    return True


class ExecutionAdapter:
    def __init__(self, context, *, store=None, runner=None, identity_verifier=None):
        self.context = copy.deepcopy(context)
        self.context.setdefault('protected_paths', []).append(str(LOCK_ROOT))
        self.store = store or (OpsStore(context['db_path']) if context.get('db_path') else None)
        self.runner = runner or run_process
        self.identity_verifier = identity_verifier

    def cancelled(self):
        return bool(self.context.get('cancelled') or (self.store and self.context.get('run_id') and self.store.is_cancelled(self.context['run_id'])))

    def _emit(self, kind, payload):
        if self.store and self.context.get('run_id'):
            self.store.emit(self.context['run_id'], kind, _redact(payload))

    def _verify_identity(self):
        if self.identity_verifier is None:
            raise RuntimeError('verified personal identity provider is unavailable')
        owner = self.identity_verifier(self.context)
        if not owner or owner != self.context.get('sender') or owner != self.context.get('user_identity_open_id'):
            raise RuntimeError('current verified login does not match sender and configured owner')

    def call(self, name, input):
        call_id = uuid.uuid4().hex
        metadata = dict(call_id=call_id, tool=name)
        # Do not persist potentially giant requests or document contents.
        self._emit('tool_requested', metadata)
        try:
            context = dict(self.context, cancelled=self.cancelled())
            decision = check_tool(name, input, context)
            if not decision['allowed']:
                self._emit('policy_denied', dict(metadata, reason=decision['reason'], success=False))
                return dict(call_id=call_id, success=False, error=decision['reason'])
            if decision.get('identity') == 'user':
                self._verify_identity()
            self._emit('policy_allowed', dict(metadata, capability=decision['capability']))
            if decision.get('p4'):
                lease = self.context.get('workspace_lease')
                if lease is not None:
                    if not validate_workspace_lease(self.context['workspace'], lease):
                        raise RuntimeError('workspace lease is expired or invalid')
                    result = self._execute(name, input)
                else:
                    with workspace_lock(self.context['workspace'], timeout=30, cancelled=self.cancelled):
                        result = self._execute(name, input)
            else:
                result = self._execute(name, input)
            clean = _redact(result)
            self._emit('tool_result' if result['success'] else 'tool_failed', dict(metadata, **clean))
            return dict(call_id=call_id, **clean)
        except Exception as exc:
            result = dict(success=False, error=str(exc)[:4000])
            self._emit('tool_failed', dict(metadata, **result))
            return dict(call_id=call_id, **_redact(result))

    def _execute(self, name, input):
        # Recheck paths, schema and cancellation after waiting/identity/trace.
        decision = check_tool(name, input, dict(self.context, cancelled=self.cancelled()))
        if not decision['allowed']:
            raise RuntimeError(decision['reason'])
        if name == 'Bash':
            code, stdout, stderr = self.runner(decision['argv'], cwd=self.context['workspace'], timeout=min(120, max(1, self.context.get('tool_timeout', 60))), cancelled=self.cancelled)
            if len(stdout.encode()) + len(stderr.encode()) > MAX_OUTPUT * 2:
                raise RuntimeError('tool output limit exceeded')
            if decision['argv'][0] == 'lark-cli':
                try:
                    parsed = json.loads(stdout)
                except (ValueError, TypeError):
                    return dict(success=False, error='lark returned malformed JSON', exit_code=code, stderr=stderr[:4000])
                return dict(success=code == 0 and isinstance(parsed, dict) and business_success(parsed), result=parsed, exit_code=code, stderr=stderr[:4000])
            return dict(success=code == 0, result=stdout, exit_code=code, stderr=stderr[:4000])
        path = Path(decision['path'])
        if name == 'Read':
            with path.open('rb') as file:
                data = file.read(MAX_OUTPUT + 1)
            if len(data) > MAX_OUTPUT:
                raise RuntimeError('file exceeds read limit')
            return dict(success=True, result=data.decode('utf-8'))
        if path.exists() and path.stat().st_nlink > 1:
            raise RuntimeError('hard-linked writes are forbidden')
        content = input.get('content')
        if name == 'Edit':
            if path.stat().st_size > MAX_INPUT:
                raise RuntimeError('file exceeds edit limit')
            original = path.read_text(encoding='utf-8')
            old = input['old_string']
            if not old or original.count(old) != 1:
                raise RuntimeError('edit requires exactly one nonempty match')
            content = original.replace(old, input['new_string'], 1)
        if len(content.encode('utf-8')) > MAX_INPUT:
            raise RuntimeError('write exceeds size limit')
        # No implicit directory creation or broad tree mutation.
        path.write_text(content, encoding='utf-8')
        return dict(success=True, result={'path': str(path), 'bytes': len(content.encode('utf-8'))})
