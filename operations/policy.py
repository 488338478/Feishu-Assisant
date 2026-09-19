"""Pure, closed registry for the operations MCP execution boundary."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

MAX_INPUT = 100_000
_CONTROL = {'.git', '.claude', '.codex', '.ssh', '.aws', '.config', '.lark', 'node_modules', '__pycache__', 'data'}
_SECRET_NAMES = {'claude.md', 'agents.md', 'skill.md', 'credentials.json', 'credentials', 'config.toml', 'settings.json', 'runtime_config.json'}

# Values are capabilities, permitted value flags, and required flags. No raw API.
LARK_OPERATIONS = {
    ('drive', '+search'): ('lark.read', {'--query', '--page-size', '--page-token', '--type'}, {'--query'}),
    ('docs', '+fetch'): ('lark.read', {'--doc', '--scope', '--keyword', '--detail', '--max-depth', '--start-block-id', '--end-block-id', '--context-before', '--context-after', '--doc-format'}, {'--doc'}),
    ('docs', '+create'): ('lark.write', {'--title', '--content', '--doc-format', '--parent-token', '--parent-position'}, set()),
    ('docs', '+update'): ('lark.write', {'--doc', '--command', '--content', '--pattern', '--block-id', '--start-block-id', '--end-block-id', '--doc-format', '--revision-id'}, {'--doc', '--command', '--content'}),
    ('calendar', '+agenda'): ('lark.read', {'--start', '--end', '--calendar-id'}, set()),
    ('calendar', '+get'): ('lark.read', {'--calendar-id', '--event-id'}, {'--event-id'}),
    ('task', '+get-my-tasks'): ('lark.read', {'--page-size', '--page-token'}, set()),
}


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def checked_path(value, context, *, write=False, tree=False) -> Path:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError('a bounded file path is required')
    if any(c in value for c in '\x00\r\n*?') or value.startswith(('\\\\', '//', '~')):
        raise ValueError('network, wildcard, and alias paths are not supported')
    root = Path(context['workspace']).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    # Windows alternate streams and Win32 trailing-dot/space aliases are forbidden.
    if any(':' in part or part.endswith((' ', '.')) for part in path.parts[1:]):
        raise ValueError('ambiguous path syntax')
    path = path.resolve()
    if not _inside(path, root):
        raise ValueError('path escapes the configured workspace')
    protected = [Path(__file__).resolve().parents[1]]
    protected += [Path(p).resolve() for p in context.get('protected_paths', [])]
    protected += [Path(p).resolve() for p in (context.get('db_path'), os.environ.get('ASSISTANT_DATA_DIR')) if p]
    for p in protected:
        if _inside(path, p) or ((write or tree) and _inside(p, path)):
            raise ValueError('path is protected assistant code or control data')
    parts = {part.lower() for part in path.relative_to(root).parts}
    if parts & (_CONTROL | _SECRET_NAMES) or any(p.startswith('.env') or p.endswith(('.pem', '.key', '.pfx', '.p12')) for p in parts):
        raise ValueError('secret, policy, or control path is protected')
    if write and path == root:
        raise ValueError('cannot replace workspace root')
    return path


def _require(capability, context):
    if capability not in context.get('capabilities', []):
        raise ValueError(f'missing capability: {capability}')
    if capability in {'file.write', 'p4.edit', 'p4.submit'}:
        minimum = 2 if capability == 'p4.submit' else 1
        if {'read': 0, 'edit': 1, 'submit': 2}.get(context.get('tier', 'read'), -1) < minimum:
            raise ValueError(f'tier does not allow {capability}')


def _options(tokens, flags):
    result = {}
    while tokens:
        key = tokens.pop(0)
        if key not in flags or key in result or not tokens:
            raise ValueError(f'unsupported, duplicate, or valueless flag: {key}')
        value = tokens.pop(0)
        if value.startswith(('@', '--')) or value == '-':
            raise ValueError('file/stdin inputs and flag-valued arguments are forbidden')
        result[key] = value
    return result


def _lark(tokens, context):
    global_options = {}
    rest = []
    while tokens:
        token = tokens.pop(0)
        if token in {'--profile', '--as'}:
            if token in global_options or not tokens:
                raise ValueError('duplicate or missing identity/profile')
            global_options[token] = tokens.pop(0)
        else:
            rest.append(token)
    profile = context.get('lark_profile', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', profile):
        raise ValueError('configured lark profile is missing or invalid')
    if global_options.get('--profile', profile) != profile:
        raise ValueError('profile override is forbidden')
    identity = global_options.get('--as')
    if identity not in {'bot', 'user'}:
        raise ValueError('explicit --as bot or --as user is required')
    operation = tuple(rest[:2])
    if operation not in LARK_OPERATIONS:
        raise ValueError('lark operation is not registered')
    capability, flags, required = LARK_OPERATIONS[operation]
    values = _options(rest[2:], flags)
    if not required <= values.keys():
        raise ValueError('required operation flags are missing')
    if operation == ('docs', '+create') and not ({'--title', '--content'} & values.keys()):
        raise ValueError('document title or content is required')
    if '--doc-format' in values and values['--doc-format'] not in {'xml', 'markdown'}:
        raise ValueError('unsupported document format')
    if operation == ('docs', '+update'):
        command = values['--command']
        if command not in {'append', 'str_replace', 'block_insert_after', 'block_replace'}:
            raise ValueError('document update command is not registered')
        if command == 'str_replace' and not values.get('--pattern'):
            raise ValueError('replacement pattern required')
        if command.startswith('block_') and not values.get('--block-id'):
            raise ValueError('registered block updates require one block-id')
    # CLI markup can load local media; this bounded adapter permits text only.
    content = values.get('--content', '')
    if re.search(r'(?i)(?:file:|@\.?[/\\]|<(?:img|image|source|attachment|include)\b|!\[)', content):
        raise ValueError('local media/reference content is unsupported')
    _require(capability, context)
    if identity == 'user':
        _require('lark.personal', context)
        if not context.get('sender') or context.get('sender') != context.get('user_identity_open_id'):
            raise ValueError('personal identity is not bound to this sender')
    argv = ['lark-cli', '--profile', profile, '--as', identity, *operation]
    for key, value in values.items():
        argv.extend([key, value])
    return dict(argv=argv, capability=capability, identity=identity)


def _p4(tokens, context):
    if not tokens:
        raise ValueError('p4 operation required')
    verb = tokens.pop(0)
    caps = {v: 'p4.read' for v in ('info', 'where', 'files', 'have', 'opened', 'filelog')}
    caps.update(edit='p4.edit', submit='p4.submit')
    if verb not in caps:
        raise ValueError('p4 operation is not registered')
    client = context.get('p4_client', '')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', client):
        raise ValueError('explicit configured p4_client is required')
    _require(caps[verb], context)
    args = []
    if verb == 'submit':
        if len(tokens) != 3 or tokens[0] != '-d' or not tokens[1] or tokens[1].startswith('-'):
            raise ValueError('submit requires -d description and exactly one local file; changelist submission unsupported')
        args = tokens[:2]
        tokens = tokens[2:]
    if verb == 'info':
        if tokens:
            raise ValueError('p4 info takes no flags')
    else:
        if not tokens:
            raise ValueError('p4 requires explicit workspace file targets')
        for token in tokens:
            if token.startswith('-') or any(c in token for c in '@#%*') or '...' in token:
                raise ValueError('p4 flags, revisions, wildcards, and indirect paths are unsupported')
            args.append(str(checked_path(token, context, write=verb in {'edit', 'submit'})))
    return dict(argv=['p4', '-c', client, '-d', str(Path(context['workspace']).resolve()), verb, *args], capability=caps[verb], p4=True)


def check_tool(name, input, context):
    """Return a decision plus canonical execution arguments; never execute anything."""
    try:
        if not isinstance(input, dict) or not isinstance(context, dict):
            raise ValueError('tool input/context must be objects')
        if context.get('cancelled'):
            raise ValueError('run cancelled')
        if name == 'Bash':
            if set(input) != {'command'} or not isinstance(input['command'], str):
                raise ValueError('Bash accepts only command string')
            command = input['command']
            if len(command) > MAX_INPUT or any(c in command for c in '\x00\r\n;|&`$<>'):
                raise ValueError('shell composition or control syntax is forbidden')
            lexer = shlex.shlex(command, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ''
            lexer.escape = ''  # Preserve native Windows paths; shell escaping is not supported.
            tokens = list(lexer)
            if not tokens or tokens[0] not in {'lark-cli', 'p4'}:
                raise ValueError('executable is not registered')
            decision = _lark(tokens[1:], context) if tokens[0] == 'lark-cli' else _p4(tokens[1:], context)
        elif name in {'Read', 'Write', 'Edit'}:
            fields = {'Read': {'file_path'}, 'Write': {'file_path', 'content'}, 'Edit': {'file_path', 'old_string', 'new_string'}}[name]
            if set(input) != fields or any(not isinstance(v, str) or len(v) > MAX_INPUT for v in input.values()):
                raise ValueError('unsupported file operation schema or input limit')
            capability = 'file.read' if name == 'Read' else 'file.write'
            _require(capability, context)
            decision = dict(path=str(checked_path(input['file_path'], context, write=name != 'Read')), capability=capability)
        else:
            raise ValueError('tool is not registered (supported: Bash, Read, Write, Edit)')
        return dict(allowed=True, reason='registered operation authorized', **decision)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        return dict(allowed=False, reason=str(exc))
