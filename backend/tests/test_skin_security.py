import base64
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from import_skins import import_pack, pack_path, reject_link


WEBP = base64.b64decode('UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA')


class SkinImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.src = self.base / 'pack'
        self.dst = self.base / 'published'
        (self.src / 'web').mkdir(parents=True)
        (self.src / 'web' / 'wall #1.webp').write_bytes(WEBP)
        self.skin = {'skinId': 'one', 'webp': 'web/wall #1.webp',
                     'thumbnail': r'web\wall #1.webp'}

    def index(self, *skins):
        data = {'roles': [{'roleId': 'sample', 'skins': list(skins)}]}
        (self.src / 'skins-index.json').write_text(json.dumps(data), encoding='utf-8')

    def test_nested_assets_and_missing_thumbnail(self):
        self.index(self.skin, dict(self.skin, thumbnail='missing.webp'))
        self.assertEqual(import_pack(self.src, self.dst), (1, 1))
        item = json.loads((self.dst / 'skins.json').read_text(encoding='utf-8'))['skins'][0]
        self.assertEqual(item['web'], '/static/skins/wall%20%231.webp')
        self.assertEqual((self.dst / 'thumbs' / 'wall #1.webp').read_bytes(), WEBP)

    def test_only_pack_relative_paths(self):
        for name in ('../a.webp', r'a\..\b.webp', '/a.webp', r'C:\a.webp',
                     'C:a.webp', r'\\host\pack\a.webp', 'a.webp:stream', 'NUL.webp'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                pack_path(self.src, name)

    def test_invalid_image_does_not_publish_partial_pack(self):
        self.index(self.skin)
        import_pack(self.src, self.dst)
        old_index = (self.dst / 'skins.json').read_bytes()
        (self.src / 'next.webp').write_bytes(WEBP)
        (self.src / 'empty.webp').write_bytes(b'')
        for name, data in [('empty.webp', b''), ('notes.txt', WEBP)]:
            (self.src / name).write_bytes(data)
            self.index(dict(self.skin, webp='next.webp'), dict(self.skin, webp=name))
            with self.subTest(name=name), self.assertRaises(ValueError):
                import_pack(self.src, self.dst)
            self.assertFalse((self.dst / 'next.webp').exists())
            self.assertEqual((self.dst / 'skins.json').read_bytes(), old_index)

    def test_reparse_points_are_rejected(self):
        info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with patch.object(Path, 'lstat', return_value=info), self.assertRaises(ValueError):
            reject_link(self.src)

    def test_existing_destination_links_do_not_modify_other_files(self):
        self.index(self.skin)
        (self.dst / 'thumbs').mkdir(parents=True)
        original = self.base / 'original'
        original.write_text('original')
        for name in ('skins.json', 'wall #1.webp', 'thumbs/wall #1.webp'):
            os.link(original, self.dst / name)
        import_pack(self.src, self.dst)
        self.assertEqual(original.read_text(), 'original')

    def test_hardlinked_pack_image_is_not_copied(self):
        outside = self.base / 'private.webp'
        outside.write_bytes(WEBP)
        os.link(outside, self.src / 'linked.webp')
        self.index(dict(self.skin, webp='linked.webp'))
        with self.assertRaises(ValueError):
            import_pack(self.src, self.dst)
        self.assertFalse((self.dst / 'linked.webp').exists())


if __name__ == '__main__':
    unittest.main()
