from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from note_sync_hub.adapters.base import AdapterError
from note_sync_hub.adapters.joplin import JoplinAdapter
from note_sync_hub.adapters.obsidian import ObsidianAdapter
from note_sync_hub.adapters.siyuan import (
    CONTAINER_ATTR,
    GLOBAL_ID_ATTR,
    TAGS_ATTR,
    SiYuanAdapter,
)
from note_sync_hub.attachments import bytes_sha256, canonical_asset_uri
from note_sync_hub.config import AppConfig
from note_sync_hub.metadata import extract_joplin_metadata
from note_sync_hub.models import Asset, Endpoint, Note


def source_note(endpoint=Endpoint.JOPLIN, *, body="正文\n", assets=None, tags=()):
    return Note(
        endpoint=endpoint,
        native_id="source",
        global_id="",
        title="目标笔记",
        folder="Knowledge/Parent",
        body=body,
        tags=tags,
        revision="1",
        assets=assets or {},
    )


class StubSiYuanAdapter(SiYuanAdapter):
    def __init__(self):
        super().__init__(AppConfig(siyuan_token="token"))
        self.calls = []

    def _request(self, path, payload=None, *, binary=False):
        payload = payload or {}
        self.calls.append((path, payload, binary))
        if path == "/api/notebook/lsNotebooks":
            return {"notebooks": [{"id": "box-1", "name": "Knowledge", "closed": False}]}
        if path == "/api/query/sql":
            return [{
                "id": "doc-1",
                "box": "box-1",
                "hpath": "/Parent/文档",
                "content": "文档",
                "updated": "20260718120000",
                "ial": "",
                "path": "/doc-1.sy",
            }]
        if path == "/api/attr/getBlockAttrs":
            return {GLOBAL_ID_ATTR: "group-1", TAGS_ATTR: '["工作", "资料"]'}
        if path == "/api/export/exportMdContent":
            return {"hPath": "/Parent/文档", "content": "正文\n\n![图](assets/图.png)\n"}
        if path == "/api/file/getFile":
            return b"image-data"
        if path == "/api/filetree/getIDsByHPath":
            return []
        if path == "/api/filetree/createDocWithMd":
            return "parent-id" if payload["path"] == "/Parent" else "target-id"
        if path in {
            "/api/attr/setBlockAttrs",
            "/api/block/updateBlock",
            "/api/filetree/moveDocsByID",
            "/api/filetree/renameDocByID",
        }:
            return None
        raise AssertionError(f"未处理的思源 API：{path}")

    def _upload_asset(self, asset):
        asset.load()
        return "assets/uploaded.png"


class SiYuanAdapterTests(unittest.TestCase):
    def test_scan_reads_hierarchy_tags_and_attachment(self):
        adapter = StubSiYuanAdapter()
        notes = adapter.list_notes()
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note.global_id, "group-1")
        self.assertEqual(note.folder, "Knowledge/Parent")
        self.assertEqual(note.title, "文档")
        self.assertEqual(note.tags, ("工作", "资料"))
        digest = bytes_sha256(b"image-data")
        self.assertIn(canonical_asset_uri(digest, "图.png"), note.body)
        self.assertEqual(note.assets[digest].source_ref, "/data/assets/图.png")

    def test_create_builds_parent_document_and_sets_sync_attributes(self):
        adapter = StubSiYuanAdapter()
        digest = bytes_sha256(b"asset")
        asset = Asset(digest=digest, filename="图.png", size=5, _data=b"asset")
        source = source_note(
            body=f"![图]({canonical_asset_uri(digest, '图.png')})\n",
            assets={digest: asset},
            tags=("工作",),
        )
        native_id = adapter.upsert_note(source, None, "Knowledge/Parent", "new-group")
        self.assertEqual(native_id, "target-id")
        creates = [payload for path, payload, _binary in adapter.calls if path == "/api/filetree/createDocWithMd"]
        self.assertEqual([item["path"] for item in creates], ["/Parent", "/Parent/目标笔记"])
        self.assertEqual(creates[-1]["markdown"], "![图](assets/uploaded.png)\n")
        attrs = [payload for path, payload, _binary in adapter.calls if path == "/api/attr/setBlockAttrs"]
        self.assertEqual(attrs[0]["attrs"], {CONTAINER_ATTR: "1"})
        self.assertEqual(attrs[-1]["attrs"][GLOBAL_ID_ATTR], "new-group")
        self.assertEqual(attrs[-1]["attrs"][TAGS_ATTR], '["工作"]')

    def test_siyuan_delete_is_deliberately_disabled(self):
        adapter = StubSiYuanAdapter()
        with self.assertRaisesRegex(AdapterError, "不会自动删除思源"):
            adapter.move_to_trash(source_note(Endpoint.SIYUAN))

    def test_create_refuses_to_overwrite_an_unrelated_siyuan_document(self):
        adapter = StubSiYuanAdapter()
        adapter._ids_by_hpath = lambda _notebook, hpath: ["occupied"] if hpath == "Parent/目标笔记" else []
        with self.assertRaisesRegex(AdapterError, "已有未关联文档"):
            adapter.upsert_note(source_note(), None, "Knowledge/Parent", "new-group")


class RenderingTests(unittest.TestCase):
    def test_platform_default_roots_and_obsidian_folder_sanitizing(self):
        joplin = JoplinAdapter(AppConfig(joplin_default_notebook="默认 Joplin"))
        siyuan = SiYuanAdapter(AppConfig(siyuan_default_notebook="默认思源"))
        with tempfile.TemporaryDirectory() as temporary:
            obsidian = ObsidianAdapter(AppConfig(obsidian_vault_path=temporary))
            self.assertEqual(obsidian.normalize_target_folder("../非法:目录"), "未命名/非法_目录")
            target = obsidian._target_path("../非法:目录", "笔记")
            target.resolve().relative_to(Path(temporary).resolve())
        self.assertEqual(joplin.normalize_target_folder(""), "默认 Joplin")
        self.assertEqual(siyuan.normalize_target_folder(""), "默认思源")

    def test_joplin_reuses_existing_resource_with_same_digest(self):
        digest = bytes_sha256(b"asset")
        asset = Asset(digest=digest, filename="图.png", size=5, _data=b"asset")
        source = source_note(
            Endpoint.OBSIDIAN,
            body=f"![图]({canonical_asset_uri(digest, '图.png')})\n",
            assets={digest: asset},
        )
        existing_asset = Asset(
            digest=digest,
            filename="old.png",
            size=5,
            source_ref="0123456789abcdef0123456789abcdef",
            _data=b"asset",
        )
        existing = source_note(Endpoint.JOPLIN, assets={digest: existing_asset})
        adapter = JoplinAdapter(AppConfig(joplin_token="token"))
        body = adapter._render_body(source, existing, "group")
        self.assertIn(":/0123456789abcdef0123456789abcdef", body)
        self.assertEqual(extract_joplin_metadata(body).global_id, "group")

    def test_obsidian_write_then_scan_preserves_canonical_signature(self):
        with tempfile.TemporaryDirectory() as temporary:
            digest = bytes_sha256(b"asset")
            asset = Asset(digest=digest, filename="图.png", size=5, _data=b"asset")
            source = source_note(
                body=f"正文\n\n![图]({canonical_asset_uri(digest, '图.png')})\n",
                assets={digest: asset},
                tags=("工作", "资料"),
            )
            adapter = ObsidianAdapter(AppConfig(
                obsidian_vault_path=temporary,
                obsidian_attachments_folder="attachments",
            ))
            adapter.upsert_note(source, None, "A", "group")
            scanned = adapter.list_notes()
            self.assertEqual(len(scanned), 1)
            self.assertEqual(scanned[0].global_id, "group")
            self.assertEqual(scanned[0].content_signature, source.content_signature)
            self.assertTrue((Path(temporary) / "attachments" / "图.png").is_file())


if __name__ == "__main__":
    unittest.main()
