import base64
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

pkg = types.ModuleType('assistant')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', pkg)
from assistant.feishu.doc_images import DocImageService
from assistant.feishu.media import MediaStore
from assistant.agent import image_mcp

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1sAAAAASUVORK5CYII=')


class DocImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MediaStore(self.root / 'media', 'chat', 'sender')
        self.calls = []
        self.revision = 4
        self.sender_perm = 'edit'
        self.bad_insert = False
        self.content = '<title>策划</title><p block-id="p1">角色</p><img block-id="b1" token="imgToken" caption="主角"/>'
        self.service = DocImageService(self.store, self.root / 'pending', self.cli)

    def cli(self, args, cwd=None):
        self.calls.append(args)
        if args[:2] == ['drive', '+member-list']:
            return {'ok': True, 'data': {'items': [{'member_type': 'openid', 'member_id': 'sender', 'perm': self.sender_perm}]}}
        if args[:2] == ['docs', '+fetch']:
            return {'ok': True, 'data': {'document': {
                'document_id': 'doc1', 'revision_id': self.revision, 'content': self.content}}}
        if args[:2] == ['docs', '+media-download']:
            (Path(cwd) / args[args.index('--output') + 1]).write_bytes(PNG)
            return {'ok': True, 'data': {}}
        if args[:2] == ['docs', '+update']:
            if args[args.index('--command') + 1] == 'block_delete':
                self.content = '<title>策划</title><p block-id="p1">角色</p>'
            else:
                addition = '<img block-id="b2" token="newToken"/>'
                if args[args.index('--block-id') + 1] == 'p1':
                    self.content = self.content.replace('</p>', '</p>' + addition, 1)
                else:
                    self.content += addition
                if self.bad_insert:
                    return {'ok': True, 'data': {'result': 'success', 'updated_blocks_count': 0,
                                               'document': {'new_blocks': []}}}
            return {'ok': True, 'data': {'result': 'success', 'updated_blocks_count': 1,
                                       'document': {'new_blocks': [{'block_id': 'b2', 'block_type': 'image', 'block_token': 'newToken'}]}}}
        return {'ok': False, 'error': 'unexpected command'}

    def test_document_image_reads_exact_original_and_returns_reusable_scoped_ref(self):
        result = self.service.read_document_image('doc1', 'b1')
        self.assertEqual(base64.b64decode(result[1]['data']), PNG)
        ref = json.loads(result[0]['text'])['ref']
        self.assertEqual(base64.b64decode(self.store.read(ref)['data']), PNG)
        self.assertIn(['docs', '+media-download', '--token', 'imgToken', '--output', './original'], self.calls)

    def test_insertion_uses_registered_original_with_verified_anchor(self):
        ref = self.store.save(PNG)['ref']
        result = self.service.insert_image('doc1', ref, 'p1', caption='主角 & 玩家')
        self.assertEqual(result['new_image_blocks'], ['b2'])
        update = next(c for c in self.calls if c[:2] == ['docs', '+update'])
        self.assertEqual(update[update.index('--revision-id') + 1], '4')
        self.assertIn('path="@./original.png"', update[update.index('--content') + 1])
        self.assertIn('主角 &amp; 玩家', update[update.index('--content') + 1])

    def test_prepare_delete_never_writes_and_confirmation_is_bound_to_sender(self):
        pending = self.service.prepare_delete('doc1', 'b1')
        self.assertFalse(any(c[:2] == ['docs', '+update'] for c in self.calls))
        other = DocImageService(MediaStore(self.root / 'media', 'chat', 'other'), self.root / 'pending', self.cli)
        with self.assertRaises(ValueError):
            other.confirm_delete(pending['confirmation_id'])
        result = self.service.confirm_delete(pending['confirmation_id'])
        self.assertTrue(result['deleted'])
        self.assertEqual(result['block_id'], 'b1')
        with self.assertRaises(ValueError):
            self.service.confirm_delete(pending['confirmation_id'])

    def test_changed_document_is_not_deleted_using_old_confirmation(self):
        pending = self.service.prepare_delete('doc1', 'b1')
        self.revision = 5
        with self.assertRaisesRegex(ValueError, '变化'):
            self.service.confirm_delete(pending['confirmation_id'])
        self.assertFalse(any(c[:2] == ['docs', '+update'] for c in self.calls))

    def test_missing_anchor_and_non_image_delete_target_fail_before_writes(self):
        ref = self.store.save(PNG)['ref']
        for action in (lambda: self.service.insert_image('doc1', ref, 'missing'),
                       lambda: self.service.prepare_delete('doc1', 'p1')):
            with self.assertRaises(ValueError):
                action()
        self.assertFalse(any(c[:2] == ['docs', '+update'] for c in self.calls))

    def test_completed_insert_is_not_reported_failed_when_audit_disk_is_unavailable(self):
        ref = self.store.save(PNG)['ref']
        with patch.dict(os.environ, {'ASSISTANT_IMAGE_CHAT_ID': 'chat'}), \
             patch.object(image_mcp, 'AUDIT_LOG_FILE', self.root):
            result = image_mcp.dispatch(dict(id=1, method='tools/call', params=dict(
                name='insert_image', arguments=dict(doc='doc1', ref=ref))), self.service)
        self.assertFalse(result['result'].get('isError', False))
        self.assertEqual(sum(c[:2] == ['docs', '+update'] for c in self.calls), 1)

    def test_requester_without_document_edit_permission_cannot_insert_or_prepare_delete(self):
        self.sender_perm = 'view'
        ref = self.store.save(PNG)['ref']
        for operation in (lambda: self.service.insert_image('doc1', ref),
                          lambda: self.service.prepare_delete('doc1', 'b1')):
            with self.assertRaisesRegex(ValueError, '权限'):
                operation()
        self.assertFalse(any(c[:2] == ['docs', '+update'] for c in self.calls))

    def test_revoked_requester_edit_permission_blocks_confirmation(self):
        pending = self.service.prepare_delete('doc1', 'b1')
        self.sender_perm = 'view'
        with self.assertRaisesRegex(ValueError, '权限'):
            self.service.confirm_delete(pending['confirmation_id'])
        self.assertFalse(any(c[:2] == ['docs', '+update'] for c in self.calls))

    def test_unrelated_new_image_is_not_reported_as_verified_insertion(self):
        self.bad_insert = True
        ref = self.store.save(PNG)['ref']
        with self.assertRaises(ValueError):
            self.service.insert_image('doc1', ref)


if __name__ == '__main__':
    unittest.main()
