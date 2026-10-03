from dataclasses import replace
import base64
import json
from unittest import TestCase
from unittest.mock import Mock

from note_sync_hub.adapters.siyuan import SiYuanAdapter
from note_sync_hub.config import AppConfig
from note_sync_hub.metadata import strip_embedded_sync_metadata
from note_sync_hub.siyuan_content import comparable_html
from tests.test_adapters import StubSiYuanAdapter, source_note


class SiYuanFidelityTests(TestCase):
    def test_placeholder_normalization_keeps_code_and_emoji_joiners(self):
        text = '<p>text\u200b<code>x\u200b</code>👩\u200d💻</p><li>\u200d</li>'
        self.assertEqual(comparable_html(text), '<p>text<code>x\u200b</code>👩\u200d💻</p><li></li>')
    def test_embedded_legacy_markers_are_removed_but_code_examples_survive(self):
        body = '---\ntags: [工作]\n---\n<!-- notesynchub_id: old -->\n正文\n```html\n<!-- notesynchub_id: example -->\n```\n'
        self.assertEqual(strip_embedded_sync_metadata(body), body.replace('<!-- notesynchub_id: old -->', ''))
    def test_export_disables_generated_title_and_yaml(self):
        adapter = StubSiYuanAdapter()
        adapter._export_markdown('doc-1')
        self.assertEqual(adapter.calls[-1][1], {'id': 'doc-1', 'yfm': False, 'addTitle': False})

    def test_json_attachment_is_returned_as_file_bytes(self):
        adapter = SiYuanAdapter(AppConfig())
        for value in [{'name': 'attachment'}, [1, 2], {'code': 1, 'custom': True}, {'code': -1, 'msg': 'example', 'data': None}]:
            raw = json.dumps(value).encode()
            adapter.session.post = Mock(return_value=Mock(
                status_code=200, headers={'Content-Type': 'application/json'}, content=raw,
                json=Mock(return_value=value)))
            self.assertEqual(adapter._read_asset('/data/assets/sample.json'), raw)

    def test_missing_attachment_error_is_not_returned_as_file_data(self):
        adapter = SiYuanAdapter(AppConfig())
        adapter.session.post = Mock(return_value=Mock(
            status_code=202, headers={'Content-Type': 'application/json'},
            json=Mock(return_value={'code': 404, 'msg': 'missing', 'data': None})))
        with self.assertRaisesRegex(RuntimeError, 'missing'):
            adapter._read_asset('/data/assets/missing.json')

    def test_yaml_is_saved_losslessly_in_attributes_and_read_back(self):
        adapter = StubSiYuanAdapter()
        header = '---\r\n# Keep this comment\r\nsystem_note: true\r\nauthor: "李\u00a0娜"\r\ntags: [工作]\r\n---\r\n\r\n'
        source = source_note(body=header + '正文\n\n![图](assets/图.png)\n')
        adapter.upsert_note(source, None, 'Knowledge/Parent', 'group')
        creates = [p for path, p, _ in adapter.calls if path == '/api/filetree/createDocWithMd']
        self.assertEqual(creates[-1]['markdown'], '正文\n\n![图](assets/图.png)\n')
        attrs = [p['attrs'] for path, p, _ in adapter.calls if path == '/api/attr/setBlockAttrs'][-1]
        self.assertEqual(base64.b64decode(attrs['custom-obsidian-frontmatter-base64']).decode(), header)
        self.assertEqual(attrs['custom-obsidian-system-note'], 'true')
        adapter.attrs_by_id = {'doc-1': attrs}
        self.assertTrue(adapter.list_notes()[0].body.startswith(header + '正文\n'))

    def test_removed_yaml_clears_old_managed_attributes_only(self):
        adapter = StubSiYuanAdapter()
        existing = source_note()
        existing.native['attrs'] = {'custom-obsidian-frontmatter-base64': 'old', 'custom-obsidian-type': 'old', 'custom-user': 'keep'}
        adapter.upsert_note(source_note(body='正文'), existing, 'Knowledge/Parent', 'group')
        attrs = [p['attrs'] for path, p, _ in adapter.calls if path == '/api/attr/setBlockAttrs'][-1]
        self.assertEqual(attrs['custom-obsidian-type'], '')
        self.assertEqual(attrs['custom-obsidian-frontmatter-base64'], '')
        self.assertNotIn('custom-user', attrs)

    def test_markdown_normalization_checks_rendered_structure_not_just_text(self):
        adapter = SiYuanAdapter(AppConfig())
        source = source_note(body='- item\n')
        actual = replace(source, body='* item\n')
        adapter._request = Mock(side_effect=[
            {'html': '<ul id="20261003120000-abcdefg" updated="20261003120000"><li>item</li></ul>'},
            {'html': '<ul id="20261003120001-hijklmn" updated="20261003120001"><li>item</li></ul>'},
        ])
        self.assertTrue(adapter.matches_written(actual, source, source.folder))
        adapter._request = Mock(side_effect=[{'html': '<p>item</p>'}, {'html': '<h1>item</h1>'}])
        self.assertFalse(adapter.matches_written(actual, source, source.folder))
