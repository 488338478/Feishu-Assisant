"""Original document-image operations and host-owned deletion confirmation."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from ..config import LARK_CLI_PROFILE
from .lark_command import resolve_lark_cli
from .media import MediaStore


def image_cli(args: list[str], cwd=None, identity: str = 'bot') -> dict:
    command = [resolve_lark_cli(), '--profile', LARK_CLI_PROFILE, '--as', identity, *args]
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            encoding='utf-8', timeout=120)
    try:
        payload = json.loads(result.stdout if result.returncode == 0 else result.stderr or result.stdout)
    except (ValueError, TypeError):
        raise ValueError('飞书图片命令失败：' + (result.stderr or result.stdout or str(result.returncode))[:300])
    if result.returncode != 0 or payload.get('ok') is not True:
        raise ValueError('飞书图片命令失败：' + str(payload.get('error', {}))[:400])
    return payload


class DocumentParser(HTMLParser):
    def __init__(self, sidecar: dict):
        super().__init__(convert_charrefs=True)
        self.images, self.ids = {}, set()
        self.order, self.parents, self.stack = [], {}, []
        self.sidecar = sidecar

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        block = attrs.get('block-id') or attrs.get('id')
        if block:
            self.ids.add(block)
            self.order.append(block)
            self.parents[block] = next((b for _, b in reversed(self.stack) if b), None)
        if tag == 'img' and block:
            resource = self.sidecar.get('img', {}).get(attrs.get('ref', ''), {})
            token = attrs.get('token') or attrs.get('src') or resource.get('token')
            if token:
                self.images[block] = dict(block_id=block, token=token,
                                          caption=attrs.get('caption', ''), attrs=attrs)
        if tag not in ('img', 'br', 'hr', 'source', 'input'):
            self.stack.append((tag, block))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class DocImageService:
    def __init__(self, store: MediaStore, pending_root: Path, cli=None, identity='bot'):
        self.store = store
        self.pending_root = Path(pending_root) / store.scope
        self.identity = identity
        self.cli = cli or (lambda args, cwd=None: image_cli(args, cwd, identity))

    def fetch(self, doc: str) -> dict:
        if not isinstance(doc, str) or not doc.strip() or len(doc) > 2000:
            raise ValueError('请提供云文档链接或 token。')
        payload = self.cli(['docs', '+fetch', '--doc', doc, '--doc-format', 'xml', '--detail', 'full'])
        if payload.get('ok') is not True:
            raise ValueError('读取云文档失败：' + str(payload.get('error', '未知错误'))[:300])
        document = payload.get('data', {}).get('document', {})
        if (not document.get('document_id') or not isinstance(document.get('revision_id'), int)
                or not isinstance(document.get('content'), str)):
            raise ValueError('文档返回结构缺少 ID、版本或正文，未执行写入。')
        parser = DocumentParser(document.get('reference_map') or {})
        parser.feed(document['content'])
        return dict(document=document, images=parser.images, ids=parser.ids,
                    order=parser.order, parents=parser.parents)

    def require_edit_permission(self, document_id: str) -> None:
        payload = self.cli(['drive', '+member-list', '--token', document_id, '--type', 'docx'])
        members = payload.get('data', {}).get('items', [])
        if payload.get('ok') is not True or not any(
                member.get('member_type') == 'openid' and member.get('member_id') == self.store.sender_id
                and member.get('perm') in ('edit', 'full_access') for member in members if isinstance(member, dict)):
            raise ValueError('无法确认发起人拥有此文档的编辑权限。请将该用户添加为可编辑协作者；继承或群组权限当前不能可靠验证。')

    def list_images(self, doc: str) -> dict:
        current = self.fetch(doc)
        return dict(document_id=current['document']['document_id'],
                    images=[{k: v for k, v in image.items() if k != 'attrs'}
                            for image in current['images'].values()])

    def read_document_image(self, doc: str, block_id: str) -> list[dict]:
        current = self.fetch(doc)
        image = current['images'].get(block_id)
        if image is None:
            raise ValueError('指定 block 不是可读取的图片。')
        self.store.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='download-', dir=self.store.directory) as directory:
            self.cli(['docs', '+media-download', '--token', image['token'], '--output', './original'], cwd=directory)
            files = [p for p in Path(directory).glob('original*') if p.is_file() and not p.is_symlink()]
            if len(files) != 1 or files[0].stat().st_size > self.store.max_bytes:
                raise ValueError('未获取到可用原图或原图超过大小限制，未进行压缩。')
            item = self.store.save(files[0].read_bytes(), source='document')
        return [dict(type='text', text=json.dumps(dict(ref=item['ref'], block_id=block_id), ensure_ascii=False)),
                self.store.read(item['ref'])]

    def insert_image(self, doc: str, ref: str, after_block_id: str = '-1',
                     caption: str = '', width: int | None = None,
                     height: int | None = None, align: str = 'center') -> dict:
        if align not in ('left', 'center', 'right'):
            raise ValueError('图片对齐参数无效。')
        for size in (width, height):
            if size is not None and (type(size) is not int or not 1 <= size <= 10000):
                raise ValueError('图片显示尺寸无效。')
        if not isinstance(caption, str) or len(caption) > 2000:
            raise ValueError('图片说明过长。')
        current = self.fetch(doc)
        if after_block_id not in ('-1', '0') and after_block_id not in current['ids']:
            raise ValueError('插入位置已变化或不存在，请重新定位。')
        document_id = current['document']['document_id']
        self.require_edit_permission(document_id)
        with self.store.lease(ref) as original:
            self.store.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='insert-', dir=self.store.directory) as directory:
                shutil.copyfile(original, Path(directory) / original.name)
                attrs = dict(path='@./' + original.name, caption=caption, align=align)
                if width is not None:
                    attrs['width'] = str(width)
                if height is not None:
                    attrs['height'] = str(height)
                xml = '<img ' + ' '.join(f'{k}="{escape(v, quote=True)}"' for k, v in attrs.items()) + '/>'
                result = self.cli(['docs', '+update', '--doc', document_id, '--command', 'block_insert_after',
                                   '--block-id', after_block_id, '--doc-format', 'xml', '--content', xml,
                                   '--revision-id', str(current['document']['revision_id'])], cwd=directory)
        self._check_write(result)
        data = result.get('data', {})
        inserted = [block for block in data.get('document', {}).get('new_blocks', [])
                    if block.get('block_type') in ('image', 'img', 27)]
        if data.get('updated_blocks_count') == 0 or len(inserted) != 1 or not inserted[0].get('block_id'):
            raise ValueError('插入请求已发出，但未返回唯一新图片块；请检查文档，不要重复插入。')
        updated = self.fetch(document_id)
        created = inserted[0]
        new_id, token = created['block_id'], created.get('block_token')
        found = updated['images'].get(new_id)
        if not found or not token or found['token'] != token or new_id in current['images']:
            raise ValueError('插入请求已发出，但回读未确认指定图片 token；请检查文档，不要重复插入。')
        order, parents = updated['order'], updated['parents']
        index = order.index(new_id)
        if after_block_id not in ('-1', '0'):
            siblings = [block for block in order if parents.get(block) == parents.get(after_block_id)]
            positioned = (parents.get(new_id) == parents.get(after_block_id) and
                          siblings.index(new_id) == siblings.index(after_block_id) + 1)
        else:
            siblings = [block for block in order if parents.get(block) == parents.get(new_id)]
            # Root document/title blocks are not editable body anchors.
            positioned = siblings[-1] == new_id if after_block_id == '-1' else index <= 1
        if not positioned:
            raise ValueError('已上传图片，但回读发现位置不符，请检查文档，不要重复插入。')
        added = [new_id]
        return dict(document_id=document_id, new_image_blocks=added, verified=True)

    def prepare_delete(self, doc: str, block_id: str) -> dict:
        current = self.fetch(doc)
        image = current['images'].get(block_id)
        if image is None:
            raise ValueError('指定 block 不是可删除的图片。')
        self.require_edit_permission(current['document']['document_id'])
        self.pending_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        confirmation_id = uuid.uuid4().hex
        pending = dict(document_id=current['document']['document_id'], image=image,
                       revision_id=current['document']['revision_id'], created_at=time.time(), identity=self.identity)
        (self.pending_root / (confirmation_id + '.json')).write_text(json.dumps(pending), encoding='utf-8')
        return dict(confirmation_id=confirmation_id, document_id=pending['document_id'],
                    block_id=block_id, caption=image['caption'],
                    message=f'请由发起人回复「确认删除图片 {confirmation_id}」，或「取消删除图片 {confirmation_id}」。')

    def confirm_delete(self, confirmation_id: str, cancel: bool = False) -> dict:
        if not re.fullmatch(r'[a-f0-9]{32}', confirmation_id):
            raise ValueError('删除确认编号无效。')
        path = self.pending_root / (confirmation_id + '.json')
        claimed = self.pending_root / (confirmation_id + '.claimed')
        try:
            # O_EXCL gives confirmations a single consumer across worker threads/processes.
            fd = os.open(claimed, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except OSError as error:
            raise ValueError('当前用户没有这条待确认请求，或请求正在处理。') from error
        try:
            pending = json.loads(path.read_text(encoding='utf-8'))
            if cancel:
                return dict(cancelled=True)
            if time.time() - pending['created_at'] > 600:
                raise ValueError('删除确认已过期，请重新发起。')
            # Preserve the authenticated identity used when preparing the candidate.
            if pending.get('identity', 'bot') != self.identity:
                self.identity = pending['identity']
                self.cli = lambda args, cwd=None: image_cli(args, cwd, self.identity)
            current = self.fetch(pending['document_id'])
            self.require_edit_permission(pending['document_id'])
            block_id = pending['image']['block_id']
            if (current['document']['revision_id'] != pending['revision_id'] or
                    current['images'].get(block_id) != pending['image']):
                raise ValueError('文档已发生变化，未删除；请重新选择图片并确认。')
            result = self.cli(['docs', '+update', '--doc', pending['document_id'], '--command', 'block_delete',
                               '--block-id', block_id, '--revision-id', str(pending['revision_id'])])
            self._check_write(result)
            if block_id in self.fetch(pending['document_id'])['images']:
                raise ValueError('删除请求已发出，但回读仍存在该图片，请检查文档。')
            return dict(deleted=True, document_id=pending['document_id'], block_id=block_id)
        except (FileNotFoundError, json.JSONDecodeError) as error:
            raise ValueError('当前用户没有这条待确认请求。') from error
        finally:
            path.unlink(missing_ok=True)
            claimed.unlink(missing_ok=True)

    @staticmethod
    def _check_write(payload):
        if payload.get('ok') is not True or payload.get('data', {}).get('result') in ('failed', 'partial_success'):
            raise ValueError('图片写操作未完整成功，请检查文档：' + str(payload.get('error') or payload.get('data'))[:300])


def cleanup_pending(root: Path, max_age: int = 600) -> int:
    root = Path(root).resolve()
    removed, now = 0, time.time()
    if not root.exists():
        return removed
    for scope in root.iterdir():
        if scope.is_symlink() or not scope.is_dir() or not re.fullmatch(r'[a-f0-9]{64}', scope.name):
            continue
        for path in scope.iterdir():
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r'[a-f0-9]{32}\.(json|claimed)', path.name):
                continue
            try:
                # Leave live claims alone; stale claims from crashes expire with the request.
                claim = path.with_suffix('.claimed')
                if claim.exists() and now - claim.stat().st_mtime < 600:
                    continue
                if now - path.stat().st_mtime > max_age:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    return removed
