"""Scoped original-image storage shared by the host and its MCP subprocess."""
import base64
import hashlib
import json
import re
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from lark_oapi.api.im.v1 import GetMessageResourceRequest, GetMessageRequest


def image_type(data: bytes) -> tuple[str, str]:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif', '.gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    raise ValueError('不支持的图片格式，请提供 PNG、JPEG、GIF 或 WebP 原图。')


class MediaStore:
    def __init__(self, root: Path, chat_id: str, sender_id: str,
                 max_bytes: int = 20 * 1024 * 1024, max_age: int = 86400):
        self.root = Path(root).resolve()
        self.scope = hashlib.sha256(json.dumps([chat_id, sender_id]).encode()).hexdigest()
        self.sender_id = sender_id
        self.directory = self.root / self.scope
        self.max_bytes, self.max_age = max_bytes, max_age

    def save(self, data: bytes, message_id: str = '', source: str = 'message') -> dict:
        if not data or len(data) > self.max_bytes:
            raise ValueError(f'原图超过允许大小（{self.max_bytes // 1024 // 1024} MiB）或为空，未进行压缩。')
        mime, extension = image_type(data)
        ref = uuid.uuid4().hex
        directory = self.directory / ref
        directory.mkdir(parents=True, mode=0o700)
        (directory / ('original' + extension)).write_bytes(data)
        item = dict(ref=ref, mime_type=mime, file_name='original' + extension,
                    message_id=message_id, source=source, size=len(data), created_at=time.time())
        (directory / 'meta.json').write_text(json.dumps(item), encoding='utf-8')
        return item

    def locate(self, ref: str) -> tuple[dict, Path]:
        if not isinstance(ref, str) or not re.fullmatch(r'[a-f0-9]{32}', ref):
            raise ValueError('图片引用无效。')
        directory = self.directory / ref
        if directory.is_symlink() or self.directory.is_symlink():
            raise ValueError('图片引用路径无效。')
        try:
            meta_path = directory / 'meta.json'
            if meta_path.is_symlink():
                raise ValueError('图片引用路径无效。')
            item = json.loads(meta_path.read_text(encoding='utf-8'))
            if time.time() - item['created_at'] > self.max_age:
                raise ValueError('图片引用已过期，请重新发送原图。')
            path = directory / item['file_name']
            if (path.is_symlink() or not path.is_file() or
                    path.resolve().parent != directory.resolve() or
                    path.stat().st_size > self.max_bytes):
                raise ValueError('图片引用路径或大小无效。')
            return item, path
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError('当前发送者在此会话中没有这张图片，请重新发送。') from error

    def read(self, ref: str) -> dict:
        _, path = self.locate(ref)
        data = path.read_bytes()
        mime, _ = image_type(data)
        return dict(type='image', mimeType=mime, data=base64.b64encode(data).decode('ascii'))

    def recent(self, limit: int = 5) -> list[dict]:
        items = []
        if not self.directory.exists():
            return items
        for directory in self.directory.iterdir():
            try:
                item, _ = self.locate(directory.name)
                items.append(item)
            except ValueError:
                continue
        return sorted(items, key=lambda item: item['created_at'], reverse=True)[:limit]

    @contextmanager
    def lease(self, ref: str):
        _, path = self.locate(ref)
        marker = path.parent / ('lease-' + uuid.uuid4().hex)
        marker.touch()
        try:
            yield path
        finally:
            marker.unlink(missing_ok=True)


def cleanup_media(root: Path, max_age: int = 86400) -> int:
    """Remove expired owned refs; leases protect work running in another process."""
    root = Path(root).resolve()
    count, now = 0, time.time()
    if not root.exists():
        return count
    for scope in root.iterdir():
        if scope.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}', scope.name) or not scope.is_dir():
            continue
        for directory in scope.iterdir():
            if directory.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', directory.name) or not directory.is_dir():
                continue
            try:
                meta = json.loads((directory / 'meta.json').read_text(encoding='utf-8'))
                if now - meta['created_at'] < max_age:
                    continue
                if any(now - p.stat().st_mtime < 3600 for p in directory.glob('lease-*')):
                    continue
                shutil.rmtree(directory)
                count += 1
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return count


def parse_message(message_type: str, content: dict) -> tuple[str, list[str]]:
    if not isinstance(content, dict):
        return '', []
    if message_type == 'text':
        return str(content.get('text') or ''), []
    if message_type == 'image':
        key = content.get('image_key')
        return '', [key] if isinstance(key, str) and key else []
    if message_type != 'post':
        return '', []
    body = content if 'content' in content else content.get('zh_cn', content.get('en_us', {}))
    if not isinstance(body, dict):
        return '', []
    text, keys = [str(body.get('title') or '')], []
    for row in body.get('content', []):
        for element in row if isinstance(row, list) else []:
            if not isinstance(element, dict):
                continue
            if element.get('tag') == 'img' and isinstance(element.get('image_key'), str):
                keys.append(element['image_key'])
            elif element.get('tag') in ('text', 'a'):
                text.append(str(element.get('text') or ''))
    return '\n'.join(text).strip(), list(dict.fromkeys(keys))


def download_message_image(client, message_id: str, image_key: str, store: MediaStore) -> dict:
    if not message_id or not image_key:
        raise ValueError('message_id / image_key 不能为空。')
    request = GetMessageResourceRequest.builder().message_id(message_id).file_key(image_key).type('image').build()
    response = client.im.v1.message_resource.get(request)
    if not response.success() or response.file is None:
        raise ValueError('无法下载飞书原图，请检查消息资源读取权限。')
    data = response.file.read(store.max_bytes + 1)
    return store.save(data, message_id)


def quoted_images(client, message_id: str, chat_id: str) -> list[str]:
    response = client.im.v1.message.get(GetMessageRequest.builder().message_id(message_id).build())
    if not response.success() or response.data is None:
        raise ValueError('无法读取引用的图片消息。')
    for item in response.data.items or []:
        if item.message_id == message_id and item.chat_id == chat_id:
            return parse_message(item.msg_type, json.loads(item.body.content))[1]
    raise ValueError('引用消息不属于当前会话。')
