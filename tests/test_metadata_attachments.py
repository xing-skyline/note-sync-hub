from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from note_sync_hub.attachments import (
    attachment_references,
    bytes_sha256,
    canonical_asset_digest,
    canonical_asset_uri,
    replace_canonical_asset_uris,
)
from note_sync_hub.config import AppConfig
from note_sync_hub.metadata import (
    SyncMetadata,
    apply_joplin_metadata,
    apply_obsidian_metadata,
    extract_joplin_metadata,
    extract_obsidian_metadata,
    strip_joplin_metadata,
    strip_obsidian_metadata,
)
from note_sync_hub.adapters.obsidian import ObsidianAdapter


class MetadataTests(unittest.TestCase):
    def test_legacy_notebridge_markers_are_adopted(self):
        joplin = "<!-- notebridge_id: old-id -->\n正文\n"
        obsidian = "---\nnotebridge_id: old-id\n---\n正文\n"
        self.assertEqual(extract_joplin_metadata(joplin).global_id, "old-id")
        self.assertEqual(extract_obsidian_metadata(obsidian).global_id, "old-id")

    def test_joplin_metadata_round_trip_does_not_change_body(self):
        body = "# 标题\n\n正文\n"
        rendered = apply_joplin_metadata(body, SyncMetadata.create("obsidian", "g1"))
        self.assertEqual(extract_joplin_metadata(rendered).global_id, "g1")
        self.assertEqual(strip_joplin_metadata(rendered), body)

    def test_obsidian_managed_tags_are_not_part_of_canonical_body(self):
        body = "---\naliases: [别名]\n---\n正文\n"
        rendered = apply_obsidian_metadata(
            body,
            SyncMetadata.create("joplin", "g2"),
            tags=("工作", "资料"),
        )
        canonical = strip_obsidian_metadata(rendered)
        self.assertIn("aliases:", canonical)
        self.assertNotIn("tags:", canonical)
        self.assertNotIn("notesynchub_", canonical)
        self.assertTrue(canonical.endswith("正文\n"))


class AttachmentTests(unittest.TestCase):
    def test_canonical_uri_round_trip(self):
        digest = bytes_sha256(b"image")
        uri = canonical_asset_uri(digest, "测试 图片.png")
        self.assertEqual(canonical_asset_digest(uri), digest)
        rendered = replace_canonical_asset_uris(f"![图]({uri})", {digest: "assets/image.png"})
        self.assertEqual(rendered, "![图](assets/image.png)")

    def test_code_blocks_and_note_links_are_not_attachments(self):
        body = "```md\n![x](assets/code.png)\n```\n![[普通笔记]]\n![[assets/真实图片.png]]\n"
        references = attachment_references(body)
        self.assertEqual([item.target for item in references], ["assets/真实图片.png"])

    def test_obsidian_scan_excludes_attachment_directory_and_canonicalizes_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = Path(temporary)
            assets = vault / "attachments"
            assets.mkdir()
            image = assets / "图.png"
            image.write_bytes(b"png-data")
            (assets / "不要当笔记.md").write_text("附件目录内容", encoding="utf-8")
            note_path = vault / "A.md"
            note_path.write_text("![图](attachments/%E5%9B%BE.png)\n", encoding="utf-8")
            adapter = ObsidianAdapter(AppConfig(obsidian_vault_path=temporary))
            notes = adapter.list_notes()
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0].title, "A")
            digest = bytes_sha256(b"png-data")
            self.assertIn(canonical_asset_uri(digest, "图.png"), notes[0].body)
            self.assertIn(digest, notes[0].assets)
            self.assertEqual(notes[0].native["attachment_issues"], [])


if __name__ == "__main__":
    unittest.main()
