"""Stdio MCP: only scoped original images and cloud-document image operations."""
import json
import os
import sys
from pathlib import Path

# Claude starts this file directly; preserve the package's relative imports.
if __package__ in (None, ''):
    import types
    package = types.ModuleType('assistant')
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules.setdefault('assistant', package)
    __package__ = 'assistant.agent'

from ..config import MEDIA_ROOT, IMAGE_PENDING_ROOT, MEDIA_MAX_BYTES, MEDIA_MAX_AGE_SECONDS, AUDIT_LOG_FILE
from ..feishu.media import MediaStore
from ..feishu.doc_images import DocImageService


def tool(name, description, properties, required):
    return dict(name=name, description=description, inputSchema=dict(
        type='object', properties=properties, required=required, additionalProperties=False))


STRING = dict(type='string')
TOOLS = [
    tool('read_image', 'Read an original image using its scoped reference; no file paths accepted.',
         dict(ref=STRING), ['ref']),
    tool('list_document_images', 'Fetch current document image blocks and captions. Supports docx/wiki URLs.',
         dict(doc=STRING), ['doc']),
    tool('read_document_image', 'Fetch and read the original image in a current document block. Returns image and reusable ref.',
         dict(doc=STRING, block_id=STRING), ['doc', 'block_id']),
    tool('insert_image', 'Insert a registered original image after a fresh document block; -1 appends. Verify after writing.',
         dict(doc=STRING, ref=STRING, after_block_id=STRING, caption=STRING,
              width=dict(type='integer'), height=dict(type='integer'),
              align=dict(type='string', enum=['left', 'center', 'right'])), ['doc', 'ref']),
    tool('prepare_delete_image', 'Prepare ONE image deletion and return a confirmation command for the user. Never executes deletion.',
         dict(doc=STRING, block_id=STRING), ['doc', 'block_id']),
]


def dispatch(request: dict, service: DocImageService | None) -> dict | None:
    request_id, method = request.get('id'), request.get('method')
    if method == 'notifications/initialized':
        return None
    if method == 'initialize':
        result = dict(protocolVersion=request.get('params', {}).get('protocolVersion', '2024-11-05'),
                      capabilities=dict(tools={}), serverInfo=dict(name='assistant-image', version='1.0'))
    elif method == 'tools/list':
        result = dict(tools=TOOLS)
    elif method == 'ping':
        result = {}
    elif method == 'tools/call':
        try:
            params = request.get('params') or {}
            name, args = params.get('name'), params.get('arguments') or {}
            definition = next((t for t in TOOLS if t['name'] == name), None)
            if definition is None or not isinstance(args, dict):
                raise ValueError('未知的图片工具或参数。')
            schema = definition['inputSchema']
            if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
                raise ValueError('图片工具参数不完整或包含未知字段。')
            for key, value in args.items():
                expected = schema['properties'][key]['type']
                if (expected == 'string' and not isinstance(value, str)) or (expected == 'integer' and type(value) is not int):
                    raise ValueError('图片工具参数类型无效。')
            if service is None:
                raise ValueError('未绑定当前消息发送者。')
            if name == 'read_image':
                with service.store.lease(args['ref']):
                    content = [service.store.read(args['ref'])]
            elif name == 'read_document_image':
                content = service.read_document_image(**args)
            else:
                method_name = {'list_document_images': 'list_images', 'insert_image': 'insert_image',
                               'prepare_delete_image': 'prepare_delete'}[name]
                data = getattr(service, method_name)(**args)
                content = [dict(type='text', text=json.dumps(data, ensure_ascii=False))]
            result = dict(content=content)
            if os.environ.get('ASSISTANT_IMAGE_CHAT_ID'):
                import time
                event = dict(time=int(time.time()), event='document_image_tool', tool=name,
                             chat_id=os.environ['ASSISTANT_IMAGE_CHAT_ID'],
                             sender=os.environ.get('ASSISTANT_IMAGE_SENDER_ID', ''),
                             doc=args.get('doc', ''), block_id=args.get('block_id', ''),
                             ref=args.get('ref', ''), identity=service.identity, ok=True)
                # Persist metadata only; image content and base64 never enter the audit log.
                try:
                    with AUDIT_LOG_FILE.open('a', encoding='utf-8') as audit:
                        audit.write(json.dumps(event, ensure_ascii=False) + '\n')
                except OSError:
                    # A successful write must not become a retryable tool error.
                    print('[MEDIA] audit metadata could not be saved', file=sys.stderr, flush=True)
        except Exception as error:
            result = dict(isError=True, content=[dict(type='text', text=str(error)[:600])])
    else:
        return dict(jsonrpc='2.0', id=request_id, error=dict(code=-32601, message='Unknown method'))
    return dict(jsonrpc='2.0', id=request_id, result=result)


def main():
    # MCP uses UTF-8 JSON regardless of the Windows console code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    chat = os.environ.get('ASSISTANT_IMAGE_CHAT_ID', '')
    sender = os.environ.get('ASSISTANT_IMAGE_SENDER_ID', '')
    service = None
    if chat and sender:
        service = DocImageService(MediaStore(MEDIA_ROOT, chat, sender, MEDIA_MAX_BYTES, MEDIA_MAX_AGE_SECONDS),
                                  IMAGE_PENDING_ROOT, identity=os.environ.get('ASSISTANT_IMAGE_IDENTITY', 'bot'))
    for line in sys.stdin:
        try:
            result = dispatch(json.loads(line), service)
        except Exception:
            result = dict(jsonrpc='2.0', id=None, error=dict(code=-32700, message='Invalid JSON request'))
        if result is not None:
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
