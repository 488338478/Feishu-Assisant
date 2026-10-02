import base64
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

pkg = types.ModuleType('assistant')
pkg.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules.setdefault('assistant', pkg)
from assistant.feishu import media

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR1sAAAAASUVORK5CYII=')


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = media.MediaStore(Path(self.temp.name), 'chat', 'user')

    def test_registered_image_round_trips_original_bytes(self):
        item = self.store.save(PNG, 'message')
        result = self.store.read(item['ref'])
        self.assertEqual(base64.b64decode(result['data']), PNG)
        self.assertEqual(result['mimeType'], 'image/png')
        self.assertNotIn('data', json.dumps(item))

    def test_refs_are_scoped_to_chat_and_sender_and_paths_are_rejected(self):
        item = self.store.save(PNG, 'message')
        for other in (media.MediaStore(Path(self.temp.name), 'other', 'user'),
                      media.MediaStore(Path(self.temp.name), 'chat', 'other')):
            with self.assertRaises(ValueError):
                other.read(item['ref'])
        for ref in ('../secret', '/etc/passwd', ''):
            with self.assertRaises(ValueError):
                self.store.read(ref)

    def test_non_image_and_oversize_bytes_are_rejected_without_conversion(self):
        with self.assertRaises(ValueError):
            self.store.save(b'<html>not an image</html>')
        small = media.MediaStore(Path(self.temp.name), 'chat', 'user', max_bytes=4)
        with self.assertRaises(ValueError):
            small.save(PNG)

    def test_sdk_download_uses_message_resource_and_preserves_bytes(self):
        seen = []
        def get(request):
            seen.append((request.message_id, request.file_key, request.type))
            return SimpleNamespace(success=lambda: True, file=io.BytesIO(PNG))
        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(
            message_resource=SimpleNamespace(get=get))))
        item = media.download_message_image(client, 'om_1', 'img_1', self.store)
        self.assertEqual(seen, [('om_1', 'img_1', 'image')])
        self.assertEqual(base64.b64decode(self.store.read(item['ref'])['data']), PNG)

    def test_rich_post_extracts_text_and_images_instead_of_ignoring_images(self):
        text, keys = media.parse_message('post', {'zh_cn': {'title': '截图', 'content': [[
            {'tag': 'text', 'text': '@_user_1 看这个'},
            {'tag': 'img', 'image_key': 'img_1'}]]}})
        self.assertIn('看这个', text)
        self.assertEqual(keys, ['img_1'])
        self.assertEqual(media.parse_message('image', {'image_key': 'img_2'}), ('', ['img_2']))


if __name__ == '__main__':
    unittest.main()
