import base64
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

pkg = types.ModuleType('assistant')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', pkg)
from assistant.feishu.media import MediaStore, cleanup_media
from assistant.agent.image_guard import evaluate

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1sAAAAASUVORK5CYII=')


class LifecycleTests(unittest.TestCase):
    def test_active_image_is_preserved_and_expired_image_is_removed_after_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MediaStore(root, 'chat', 'user')
            ref = store.save(PNG)['ref']
            with store.lease(ref):
                self.assertEqual(cleanup_media(root, 0), 0)
                self.assertEqual(base64.b64decode(store.read(ref)['data']), PNG)
            self.assertEqual(cleanup_media(root, 0), 1)
            with self.assertRaises(ValueError):
                store.read(ref)

    def test_cleanup_does_not_follow_symlink_outside_media_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'keep').write_text('keep')
            media = root / 'media'
            media.mkdir()
            try:
                (media / ('a' * 64)).symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('symlink privilege unavailable')
            self.assertEqual(cleanup_media(media, 0), 0)
            self.assertEqual((outside / 'keep').read_text(), 'keep')

    def test_direct_delete_cannot_bypass_host_image_confirmation(self):
        for command in ('lark-cli docs +update --doc doc --command block_delete --block-id b1',
                        'lark-cli docs +update --doc doc --command block_"delete" --block-id b1',
                        'lark-"cli" docs +update --doc doc --command block_"delete" --block-id b1',
                        'lark-cli api POST /open-apis/docx/v1/documents/doc/blocks/b1/children/batch_delete',
                        'lark-cli api DELETE /open-apis/docx/v1/documents/doc/blocks/b1/children/batch_delete',
                        'lark-cli docs +history-revert --doc doc --revision-id 1',
                        'lark-cli --profile assistant-bot docs +update --command overwrite --doc doc --content "x"'):
            self.assertFalse(evaluate({'tool_name': 'Bash', 'tool_input': {'command': command}})['allowed'])
        self.assertTrue(evaluate({'tool_name': 'Bash', 'tool_input': {
            'command': 'lark-cli docs +fetch --doc doc'}})['allowed'])
        self.assertTrue(evaluate({'tool_name': 'mcp__assistant_image__prepare_delete_image',
                                  'tool_input': {'doc': 'doc', 'block_id': 'b1'}})['allowed'])


if __name__ == '__main__':
    unittest.main()
