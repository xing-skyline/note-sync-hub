"""Content fidelity and durable pairing without markers in the user's notes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock

from note_sync_hub.adapters.joplin import JoplinAdapter
from note_sync_hub.adapters.obsidian import ObsidianAdapter
from note_sync_hub.attachments import find_attachment_references, replace_reference_targets
from note_sync_hub.config import AppConfig
from note_sync_hub.engine import SyncEngine
from note_sync_hub.metadata import (
    SyncMetadata, apply_joplin_metadata, apply_obsidian_metadata,
    strip_joplin_metadata, strip_obsidian_metadata,
)
from note_sync_hub.models import Endpoint as E, OperationAction, SyncMode, SyncOptions
from note_sync_hub.state import StateStore
from tests.test_adapters import StubSiYuanAdapter
from tests.test_engine import FakeAdapter, make_note


class MarkerlessAdapter(FakeAdapter):
    def upsert_note(self, *args):
        native_id = super().upsert_note(*args)
        next(note for note in self.notes if note.native_id == native_id).global_id = ""
        return native_id

    def set_global_id(self, *args):
        raise AssertionError("Pairing must not write to a note")


class VerbatimSyncTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = AppConfig(obsidian_vault_path=str(self.vault), joplin_token="test")
        self.obsidian = ObsidianAdapter(self.config)
        self.joplin = MarkerlessAdapter(E.JOPLIN)
        self.state = StateStore(self.root / "state.json")
        self.options = SyncOptions(mode=SyncMode.ONE_WAY, endpoints=(E.JOPLIN, E.OBSIDIAN), source=E.JOPLIN)

    def engine(self):
        return SyncEngine(self.config, adapters={E.JOPLIN: self.joplin, E.OBSIDIAN: self.obsidian}, state_store=self.state)

    def sync(self):
        engine = self.engine()
        plan = engine.preview(self.options)
        result = engine.execute(plan)
        self.assertEqual(result.errors, [])
        return plan

    def test_unmanaged_text_and_frontmatter_are_untouched(self):
        bodies = [
            "", "\r\n\r\n正文  \r\n\t缩进\r\n\r\n", "正文没有末尾换行",
            "---\r\n# 保留注释\r\ntags: ['工作', 资料]\r\naliases: [\"别名\"]\r\n---\r\n\r\n正文\r\n",
            "---\n- 用户自己的列表\n---\n\n正文\n",
            "---\nnotesynchub_example: 用户字段\n---\n正文\n",
            "```html\n<!-- notesynchub_id: documentation-example -->\n```\n",
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(strip_joplin_metadata(body), body)
                self.assertEqual(strip_obsidian_metadata(body), body)

    def test_read_and_write_obsidian_preserve_utf8_bytes(self):
        body = "\ufeff---\r\n# 注释\r\ntags: ['工作']\r\n---\r\n\r\n正文  \r\n\r\n"
        path = self.vault / "原文.md"
        path.write_bytes(body.encode("utf-8"))
        source = self.obsidian.read_note("原文.md")
        self.assertEqual(source.body, body)
        target = self.obsidian.upsert_note(replace(source, title="副本"), None, "", "group")
        self.assertEqual((self.vault / target).read_bytes(), path.read_bytes())

    def test_joplin_payload_has_only_original_body(self):
        source = make_note(E.OBSIDIAN, native_id="source", body="\n\n---\ntags: [工作]\n---\n正文  ")
        adapter = JoplinAdapter(self.config)
        adapter._ensure_notebook = Mock(return_value="folder")
        adapter._find_note_by_title = Mock(return_value=None)
        adapter._sync_tags = Mock()
        adapter._request = Mock(return_value=Mock(json=lambda: {"id": "created"}))
        adapter.upsert_note(source, None, "A", "group")
        self.assertEqual(adapter._request.call_args.kwargs["json_data"]["body"], source.body)

    def test_sync_keeps_source_unchanged_and_resumes_from_disk_state(self):
        body = "\r\n\r\n# 原标题\r\n\r\n正文  \r\n\t缩进\r\n\r\n"
        source = make_note(E.JOPLIN, native_id="j", body=body)
        source.tags = ("工作",)
        self.joplin.notes = [source]
        before = deepcopy(source)
        self.sync()
        self.assertEqual((self.vault / "A/笔记.md").read_bytes(), body.encode("utf-8"))
        self.assertEqual(self.joplin.notes, [before])
        self.state = StateStore(self.root / "state.json")
        self.obsidian = ObsidianAdapter(self.config)
        self.assertEqual(self.engine().preview(self.options).operations, [])
        self.assertEqual(len(self.state.load()["groups"]), 1)

    def test_link_only_never_writes_either_note(self):
        self.joplin.notes = [make_note(E.JOPLIN, native_id="j", folder="")]
        path = self.vault / "笔记.md"
        path.write_bytes("正文\n".encode("utf-8"))
        original_stat = path.stat()
        self.assertEqual(self.sync().operations[0].action, OperationAction.LINK)
        self.assertEqual(path.stat().st_mtime_ns, original_stat.st_mtime_ns)
        self.assertEqual(self.engine().preview(self.options).operations, [])

    def test_existing_target_markers_are_removed_on_next_one_way_sync(self):
        source = make_note(E.JOPLIN, native_id="j", folder="", global_id="legacy")
        self.joplin.notes = [source]
        path = self.vault / "笔记.md"
        path.write_bytes(apply_obsidian_metadata(source.body, SyncMetadata.create("joplin", "legacy")).encode("utf-8"))
        self.sync()
        self.assertEqual(path.read_bytes(), source.body.encode("utf-8"))
        self.assertEqual(self.engine().preview(self.options).operations, [])

    def test_legacy_source_is_read_without_rewriting_its_file(self):
        path = self.vault / "笔记.md"
        body = apply_joplin_metadata("正文\n", SyncMetadata.create("joplin", "legacy"))
        self.assertEqual(strip_joplin_metadata(body), "正文\n")
        path.write_bytes(apply_obsidian_metadata("正文\n", SyncMetadata.create("joplin", "legacy")).encode("utf-8"))
        before = path.read_bytes()
        self.options.source = E.OBSIDIAN
        self.sync()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.joplin.notes[0].body, "正文\n")
        self.assertEqual(self.engine().preview(self.options).operations, [])

    def test_newline_only_edit_is_propagated(self):
        self.joplin.notes = [make_note(E.JOPLIN, native_id="j", body="正文\n")]
        self.sync()
        self.joplin.notes[0] = replace(self.joplin.notes[0], body="正文\r\n\r\n", revision="2")
        self.assertEqual(self.sync().operations[0].action, OperationAction.UPDATE)
        self.assertEqual((self.vault / "A/笔记.md").read_bytes(), "正文\r\n\r\n".encode("utf-8"))

    def test_bidirectional_edit_and_deletion_work_without_markers(self):
        self.options = SyncOptions(mode=SyncMode.BIDIRECTIONAL, endpoints=(E.JOPLIN, E.OBSIDIAN), primary=E.JOPLIN, propagate_deletions=True)
        self.joplin.notes = [make_note(E.JOPLIN, native_id="j", body="正文\n")]
        self.sync()
        path = self.vault / "A/笔记.md"
        path.write_bytes("从目标修改\n\n".encode("utf-8"))
        self.sync()
        self.assertEqual(self.joplin.notes[0].body, "从目标修改\n\n")
        self.assertEqual(self.engine().preview(self.options).operations, [])
        self.joplin.notes.clear()
        pending = self.engine().preview(self.options).operations
        self.assertEqual([op.action for op in pending], [OperationAction.DELETE])

    def test_bidirectional_body_edit_preserves_native_tags_not_in_markdown(self):
        self.options = SyncOptions(mode=SyncMode.BIDIRECTIONAL, endpoints=(E.JOPLIN, E.OBSIDIAN), primary=E.JOPLIN)
        self.joplin.notes = [replace(make_note(E.JOPLIN, native_id="j"), tags=("原生标签",))]
        self.sync()
        (self.vault / "A/笔记.md").write_bytes("修改正文 #新增标签\n".encode("utf-8"))
        self.sync()
        self.assertEqual(set(self.joplin.notes[0].tags), {"原生标签", "新增标签"})
        self.assertEqual(self.engine().preview(self.options).operations, [])

    def test_obsidian_rename_and_edit_keep_the_same_pairing(self):
        self.options = SyncOptions(mode=SyncMode.BIDIRECTIONAL, endpoints=(E.JOPLIN, E.OBSIDIAN), primary=E.OBSIDIAN, propagate_deletions=True)
        self.joplin.notes = [make_note(E.JOPLIN, native_id="j")]
        self.sync()
        original_group = next(iter(self.state.load()["groups"]))
        path = self.vault / "A/笔记.md"
        moved = self.vault / "改名.md"
        path.rename(moved)
        moved.write_bytes("改名后修改正文\n".encode("utf-8"))
        plan = self.sync()
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].global_id, original_group)
        self.assertEqual(len(self.joplin.notes), 1)
        self.assertEqual(self.joplin.notes[0].title, "改名")
        self.assertEqual(self.joplin.notes[0].body, "改名后修改正文\n")
        self.assertEqual(self.engine().preview(self.options).operations, [])

    def test_old_signature_baseline_migrates_without_a_false_conflict(self):
        self.options = SyncOptions(mode=SyncMode.BIDIRECTIONAL, endpoints=(E.JOPLIN, E.OBSIDIAN), primary=E.JOPLIN)
        self.joplin.notes = [make_note(E.JOPLIN, native_id="j")]
        self.sync()
        groups = self.state.load()["groups"]
        for record in next(iter(groups.values()))["endpoints"].values():
            record["signature_version"] = 2
            record["signature"] = self.joplin.notes[0].legacy_content_signature
        self.state.save(groups)
        plan = self.sync()
        self.assertEqual([op.action for op in plan.operations], [OperationAction.LINK])
        self.assertEqual(self.engine().preview(self.options).operations, [])

    def test_legacy_signature_preserves_detection_of_edits_with_same_revision(self):
        source = make_note(E.JOPLIN, native_id="j", body="原文\n")
        record = {
            "signature": source.legacy_content_signature, "signature_version": 2,
            "title": source.title, "folder": source.folder, "revision": source.revision,
        }
        self.assertFalse(SyncEngine._record_changed(source, record))
        self.assertTrue(SyncEngine._record_changed(replace(source, body="修改正文\n"), record))
        self.assertTrue(SyncEngine._record_changed(replace(source, tags=("新标签",)), record))

    def test_legacy_signature_recognizes_old_frontmatter_and_link_rendering(self):
        digest = "a" * 64
        old = make_note(E.OBSIDIAN, native_id="old", body=f"---\naliases:\n- 别名\n---\n![图](notesync-asset://{digest}/image.png)")
        current = replace(old, body=f'---\r\n# 注释\r\naliases: [别名]\r\ntags: []\r\n---\r\n\r\n![ 图 ](<notesync-asset://{digest}/image.png> "说明")\r\n')
        self.assertEqual(current.legacy_content_signature, old.content_signature)

    def test_verification_rejects_newline_changes_by_non_normalizing_targets(self):
        source = make_note(E.JOPLIN, native_id="j", body="正文\r\n\r\n")
        altered = replace(source, body="正文\n")
        self.assertFalse(self.joplin.matches_written(altered, source, "A"))

    def test_strip_legacy_yaml_preserves_user_formatting(self):
        body = "---\r\n# 注释\r\ntags: [工作, '阅读']\r\nnotesynchub_id: legacy\r\ncustom: {x: 1}\r\n---\r\n\r\n正文\r\n"
        self.assertEqual(strip_obsidian_metadata(body), body.replace("notesynchub_id: legacy\r\n", ""))

    def test_markdown_link_titles_and_spacing_survive_attachment_conversion(self):
        body = '![ 图 ](<assets/pic.png> "图片说明")  \r\n'
        reference = find_attachment_references(body)[0]
        converted = replace_reference_targets(body, [(reference, ":/resource", "pic.png")])
        self.assertEqual(converted, '![ 图 ](<:/resource> "图片说明")  \r\n')

    def test_siyuan_create_does_not_add_sync_id(self):
        adapter = StubSiYuanAdapter()
        source = make_note(E.JOPLIN, native_id="j", body="\n原文\n\n")
        native_id = adapter.upsert_note(source, None, "Knowledge/Parent", "group")
        creates = [payload for path, payload, _ in adapter.calls if path == "/api/filetree/createDocWithMd"]
        self.assertEqual(creates[-1]["markdown"], source.body)
        attrs = [payload["attrs"] for path, payload, _ in adapter.calls if path == "/api/attr/setBlockAttrs" and payload["id"] == native_id]
        self.assertTrue(all(not values.get("custom-notesynchub-id") for values in attrs))
