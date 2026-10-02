"""Claude hook keeping destructive document-image edits on the host workflow."""
import json
import os
import re
import shlex
import sys
from pathlib import Path

if __package__ in (None, ''):
    import types
    pkg = types.ModuleType('assistant')
    pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules.setdefault('assistant', pkg)
    __package__ = 'assistant.agent'

from ..config import MEDIA_ROOT, IMAGE_PENDING_ROOT
from ..feishu.media import MediaStore
from ..feishu.doc_images import DocImageService


def evaluate(event: dict) -> dict:
    if event.get('tool_name') != 'Bash':
        return dict(allowed=True)
    command = str((event.get('tool_input') or {}).get('command') or '')
    try:
        tokens = shlex.split(command)
    except ValueError:
        return dict(allowed='lark' not in command, reason='无法解析飞书文档命令。')
    if not any('lark-cli' in token for token in tokens):
        return dict(allowed=True)
    # Shell quote concatenation is resolved by shlex; dynamic expansion must not
    # hide the operation that is about to run.
    if any(c in command for c in ('$', '`')):
        return dict(allowed=False, reason='图片操作不能使用动态 Shell 展开，请使用图片工具。')
    values = {token.split('=', 1)[-1] for token in tokens}
    if 'api' in values and any('/open-apis/docx/' in value for value in values) and values & {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return dict(allowed=False, reason='云文档块写操作请使用受控图片工具或 docs +update，不能通过原始 API 绕过图片确认。')
    if values & {'block_delete', 'overwrite', '+history-revert', 'DELETE'}:
        return dict(allowed=False, reason='图片删除须使用 prepare_delete_image，并由用户在飞书确认；禁止整篇覆盖。')
    if 'block_replace' in values:
        try:
            def value(flag):
                for i, token in enumerate(tokens):
                    if token == flag:
                        return tokens[i + 1]
                    if token.startswith(flag + '='):
                        return token.split('=', 1)[1]
                return ''
            doc, block = value('--doc'), value('--block-id')
            if not doc or not block or value('--start-block-id') or any(c in command for c in (';', '|', '\n')):
                raise ValueError('无法安全确定替换范围，请使用单个文本 block ID。')
            store = MediaStore(MEDIA_ROOT, os.environ.get('ASSISTANT_IMAGE_CHAT_ID', ''),
                               os.environ.get('ASSISTANT_IMAGE_SENDER_ID', ''))
            service = DocImageService(store, IMAGE_PENDING_ROOT,
                                      identity=os.environ.get('ASSISTANT_IMAGE_IDENTITY', 'bot'))
            current = service.fetch(doc)
            # Reject replacements of containers holding images as well as image blocks.
            from xml.etree import ElementTree as ET
            xml = ET.fromstring('<root>' + current['document']['content'] + '</root>')
            targets = set(block.split(','))
            for element in xml.iter():
                if (element.get('block-id') or element.get('id')) in targets:
                    if any(child.tag == 'img' for child in element.iter()):
                        raise ValueError('替换目标包含图片，请先通过图片删除确认流程处理。')
        except Exception as error:
            return dict(allowed=False, reason=str(error)[:300])
    return dict(allowed=True)


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        decision = evaluate(json.load(sys.stdin))
    except Exception:
        decision = dict(allowed=False, reason='无法解析文档图片保护请求。')
    if not decision['allowed']:
        print(json.dumps(dict(hookSpecificOutput=dict(hookEventName='PreToolUse',
            permissionDecision='deny', permissionDecisionReason=decision['reason'])), ensure_ascii=False))


if __name__ == '__main__':
    main()
