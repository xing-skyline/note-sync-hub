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
    TRASH_CONTAINER_ATTR,
    TRASH_FOLDER_TITLE,
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
        super().__init__(AppConfig(siyuan_token="token", siyuan_default_notebook="Knowledge"))
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
            attrs_by_id = getattr(self, "attrs_by_id", {})
            if payload.get("id") in attrs_by_id:
                return attrs_by_id[payload["id"]]
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

    def test_scan_excludes_structural_container_even_when_export_contains_title(self):
        adapter = StubSiYuanAdapter()
        adapter.attrs_by_id = {"doc-1": {CONTAINER_ATTR: "1"}}

        self.assertEqual(adapter.list_notes(), [])

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

    def test_siyuan_delete_moves_document_to_managed_trash(self):
        adapter = StubSiYuanAdapter()
        note = source_note(Endpoint.SIYUAN)
        note.native["notebook_id"] = "another-box"

        adapter.move_to_trash(note)

        creates = [payload for path, payload, _binary in adapter.calls if path == "/api/filetree/createDocWithMd"]
        self.assertEqual(creates[-1]["path"], f"/{TRASH_FOLDER_TITLE}")
        attrs = [payload for path, payload, _binary in adapter.calls if path == "/api/attr/setBlockAttrs"]
        self.assertEqual(attrs[-1]["attrs"][TRASH_CONTAINER_ATTR], "1")
        moves = [payload for path, payload, _binary in adapter.calls if path == "/api/filetree/moveDocsByID"]
        self.assertEqual(moves[-1], {"fromIDs": ["source"], "toID": "target-id"})

    def test_scan_and_folder_list_exclude_managed_trash_subtree(self):
        adapter = StubSiYuanAdapter()
        normal_row = adapter._document_rows()[0]
        trash_rows = [
            {
                "id": "trash-id",
                "box": "box-1",
                "hpath": "/已重命名的回收站",
                "content": "已重命名的回收站",
                "updated": "20260718120000",
                "ial": f'{{: {TRASH_CONTAINER_ATTR}="1"}}',
                "path": "/trash-id.sy",
            },
            {
                "id": "deleted-id",
                "box": "box-1",
                "hpath": "/已重命名的回收站/已删除笔记",
                "content": "已删除笔记",
                "updated": "20260718120000",
                "ial": "",
                "path": "/trash-id/deleted-id.sy",
            },
        ]
        adapter._document_rows = lambda: [normal_row, *trash_rows]

        self.assertEqual([note.native_id for note in adapter.list_notes()], ["doc-1"])
        folders = adapter.list_folders()
        self.assertNotIn("Knowledge/已重命名的回收站", folders)
        self.assertNotIn("Knowledge/已重命名的回收站/已删除笔记", folders)
        create_count = sum(path == "/api/filetree/createDocWithMd" for path, _payload, _binary in adapter.calls)
        adapter.config.siyuan_default_notebook = "另一个默认笔记本"
        self.assertEqual(adapter._ensure_trash_container(), "trash-id")
        self.assertEqual(
            sum(path == "/api/filetree/createDocWithMd" for path, _payload, _binary in adapter.calls),
            create_count,
        )

    def test_create_refuses_to_overwrite_an_unrelated_siyuan_document(self):
        adapter = StubSiYuanAdapter()
        adapter.attrs_by_id = {"occupied": {}}  # 无同步标记的无关文档
        adapter._ids_by_hpath = lambda _notebook, hpath: ["occupied"] if hpath == "Parent/目标笔记" else []
        with self.assertRaisesRegex(AdapterError, "已有未关联文档"):
            adapter.upsert_note(source_note(), None, "Knowledge/Parent", "new-group")

    def test_create_refuses_to_overwrite_a_document_with_a_different_sync_id(self):
        adapter = StubSiYuanAdapter()
        adapter.attrs_by_id = {"occupied": {GLOBAL_ID_ATTR: "other-group"}}
        adapter._ids_by_hpath = lambda _notebook, hpath: ["occupied"] if hpath == "Parent/目标笔记" else []
        with self.assertRaisesRegex(AdapterError, "已有其他同步文档"):
            adapter.upsert_note(source_note(), None, "Knowledge/Parent", "new-group")

    def test_create_claims_existing_document_with_matching_sync_id(self):
        # 状态曾丢失、引擎未提前配对时，撞上的同名文档若带相同同步 ID，
        # 应被安全接管并更新，而不是误报为未关联。
        adapter = StubSiYuanAdapter()
        adapter.attrs_by_id = {"occupied": {GLOBAL_ID_ATTR: "same-group"}}
        adapter._ids_by_hpath = lambda _notebook, hpath: ["occupied"] if hpath == "Parent/目标笔记" else []
        block_id = adapter.upsert_note(source_note(), None, "Knowledge/Parent", "same-group")
        self.assertEqual(block_id, "occupied")
        self.assertTrue(
            any(path == "/api/block/updateBlock" and payload.get("id") == "occupied"
                for path, payload, _binary in adapter.calls),
            "应更新被认领的既有文档",
        )


class StubJoplinAdapter(JoplinAdapter):
    """拦截 HTTP，模拟目标笔记本里已有笔记的场景。"""

    def __init__(self, existing_notes):
        super().__init__(AppConfig(joplin_token="token"))
        # existing_notes: List[dict(id,title,body)]，代表目标笔记本内现有笔记
        self._existing_notes = existing_notes
        self.created = []
        self.updated = []

    def _ensure_notebook(self, folder):
        return "notebook-1"

    def _render_body(self, source, existing, global_id):
        from note_sync_hub.metadata import apply_joplin_metadata, SyncMetadata
        return apply_joplin_metadata(source.body, SyncMetadata.create(source.endpoint.value, global_id))

    def _sync_tags(self, note_id, tags):
        return None

    def _paged(self, path, fields):
        if path == "/folders/notebook-1/notes":
            yield from self._existing_notes
            return
        yield from ()

    def _request(self, method, path, *, json_data=None, **kwargs):
        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def json(self):
                return self._payload

        if method == "POST" and path == "/notes":
            self.created.append(json_data)
            return _Resp({"id": "new-note-id"})
        if method == "PUT" and path.startswith("/notes/"):
            self.updated.append((path.split("/notes/")[1], json_data))
            return _Resp({"id": path.split("/notes/")[1]})
        return _Resp({})


class JoplinDedupTests(unittest.TestCase):
    def _existing(self, gid):
        from note_sync_hub.metadata import apply_joplin_metadata, SyncMetadata
        body = "正文\n" if gid is None else apply_joplin_metadata(
            "正文\n", SyncMetadata.create("obsidian", gid)
        )
        return [{"id": "occupied", "title": "目标笔记", "body": body}]

    def test_claims_existing_note_with_matching_marker(self):
        adapter = StubJoplinAdapter(self._existing("same-group"))
        note = source_note(Endpoint.OBSIDIAN, body="新正文\n")
        note.title = "目标笔记"
        note_id = adapter.upsert_note(note, None, "笔记本", "same-group")
        self.assertEqual(note_id, "occupied")
        self.assertEqual(adapter.created, [], "认领时不应新建笔记")
        self.assertEqual([nid for nid, _ in adapter.updated], ["occupied"])

    def test_refuses_note_with_different_marker(self):
        adapter = StubJoplinAdapter(self._existing("other-group"))
        note = source_note(Endpoint.OBSIDIAN, body="新正文\n")
        note.title = "目标笔记"
        with self.assertRaisesRegex(AdapterError, "已有其他同步笔记"):
            adapter.upsert_note(note, None, "笔记本", "new-group")
        self.assertEqual(adapter.created, [])

    def test_refuses_unrelated_note_without_marker(self):
        adapter = StubJoplinAdapter(self._existing(None))
        note = source_note(Endpoint.OBSIDIAN, body="新正文\n")
        note.title = "目标笔记"
        with self.assertRaisesRegex(AdapterError, "已有未关联笔记"):
            adapter.upsert_note(note, None, "笔记本", "new-group")
        self.assertEqual(adapter.created, [])

    def test_creates_new_note_when_no_collision(self):
        adapter = StubJoplinAdapter([])
        note = source_note(Endpoint.OBSIDIAN, body="新正文\n")
        note.title = "全新笔记"
        note_id = adapter.upsert_note(note, None, "笔记本", "gid")
        self.assertEqual(note_id, "new-note-id")
        self.assertEqual(len(adapter.created), 1)


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
