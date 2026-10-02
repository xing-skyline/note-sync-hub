from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from note_sync_hub.adapters.base import AdapterError
from note_sync_hub.adapters.joplin import JoplinAdapter
from note_sync_hub.adapters.obsidian import ObsidianAdapter
from note_sync_hub.config import AppConfig
from note_sync_hub.engine import SyncEngine, SyncEngineError
from note_sync_hub.gui import SyncApp
from note_sync_hub.models import Endpoint as E, OperationAction, SyncMode, SyncOptions
from note_sync_hub.state import StateStore
from tests.test_engine import FakeAdapter, MemoryState, make_note, state_record


class SafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.vault = Path(self.temporary.name)
        self.config = AppConfig(obsidian_vault_path=str(self.vault), joplin_token="test", siyuan_token="test")
        self.options = SyncOptions(mode=SyncMode.BIDIRECTIONAL, endpoints=(E.JOPLIN, E.OBSIDIAN), primary=E.JOPLIN)

    def test_directory_enumeration_error_aborts_scan(self):
        with patch("os.scandir", side_effect=PermissionError("unavailable")):
            with self.assertRaises(AdapterError):
                ObsidianAdapter(self.config).list_notes()

    def test_nested_attachment_directory_does_not_hide_parent_or_namesakes(self):
        for name in ("Notes/keep.md", "Notes/images/skip.md", "Other/images/keep.md"):
            path = self.vault / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("body", encoding="utf-8")
        adapter = ObsidianAdapter(replace(self.config, obsidian_attachments_folder="Notes/images"))
        self.assertEqual({n.native_id for n in adapter.list_notes()}, {"Notes/keep.md", "Other/images/keep.md"})

    def test_unrelated_collision_files_are_never_overwritten(self):
        for name in ("A_B.md", "A_B_12345678.md"):
            (self.vault / name).write_text("unrelated", encoding="utf-8")
        source = make_note(E.JOPLIN, native_id="j", title="A:B", body="incoming")
        with self.assertRaises(AdapterError):
            ObsidianAdapter(self.config).upsert_note(source, None, "", "12345678-group")
        for name in ("A_B.md", "A_B_12345678.md"):
            self.assertEqual((self.vault / name).read_text(encoding="utf-8"), "unrelated")

    def test_post_write_source_edit_remains_pending(self):
        old_j = make_note(E.JOPLIN, native_id="j", global_id="g", body="V1")
        old_o = make_note(E.OBSIDIAN, native_id="o", global_id="g", body="V1")
        ja = FakeAdapter(E.JOPLIN, [replace(old_j, body="V2", revision="2")])
        oa = FakeAdapter(E.OBSIDIAN, [old_o])
        state = MemoryState({"g": state_record(old_j, old_o)})
        engine = SyncEngine(self.config, adapters={E.JOPLIN: ja, E.OBSIDIAN: oa}, state_store=state)
        original_write = oa.upsert_note

        def write_then_edit(*args):
            result = original_write(*args)
            ja.notes = [replace(ja.notes[0], body="V3 user edit", revision="3")]
            return result

        oa.upsert_note = write_then_edit
        engine.execute(engine.preview(self.options))
        pending = engine.preview(self.options).executable_operations()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].source_note.body, "V3 user edit")

    def test_duplicate_identity_cannot_be_resolved_as_deleted_note(self):
        ja = FakeAdapter(E.JOPLIN, [make_note(E.JOPLIN, native_id=n, global_id="dup") for n in ("j1", "j2")])
        oa = FakeAdapter(E.OBSIDIAN)
        engine = SyncEngine(self.config, adapters={E.JOPLIN: ja, E.OBSIDIAN: oa}, state_store=MemoryState())
        plan = engine.preview(self.options)
        operation = plan.operations[0]
        app = SimpleNamespace(
            _busy=False,
            preview_tree=SimpleNamespace(selection=lambda: ["row"]),
            operation_by_iid={"row": operation}, plan=plan, plan_engine=engine,
            _render_plan=lambda: None, status_var=SimpleNamespace(set=lambda value: None),
        )
        with patch("note_sync_hub.gui.messagebox.askyesno", return_value=True), patch("note_sync_hub.gui.messagebox.showwarning"):
            SyncApp._resolve_selected_conflict(app)
        self.assertFalse(operation.executable)
        self.assertEqual(operation.action, OperationAction.CONFLICT)

    def test_one_way_delete_versus_target_edit_is_conflict(self):
        source = make_note(E.JOPLIN, native_id="j", global_id="g", body="old")
        target = make_note(E.OBSIDIAN, native_id="o", global_id="g", body="old")
        engine = SyncEngine(self.config, adapters={
            E.JOPLIN: FakeAdapter(E.JOPLIN),
            E.OBSIDIAN: FakeAdapter(E.OBSIDIAN, [replace(target, body="new target edit")]),
        }, state_store=MemoryState({"g": state_record(source, target)}))
        options = SyncOptions(mode=SyncMode.ONE_WAY, endpoints=self.options.endpoints, source=E.JOPLIN, propagate_deletions=True)
        self.assertEqual(engine.preview(options).operations[0].action, OperationAction.CONFLICT)

    def test_joplin_note_links_and_code_examples_are_not_missing_resources(self):
        adapter = JoplinAdapter(self.config)
        identifier = "a" * 32
        code_identifier = "b" * 32
        adapter._load_folders = lambda refresh=False: {}
        adapter._folder_path = lambda identifier: ""
        adapter._list_note_resources = lambda identifier: []
        adapter._note_tags = lambda identifier: ()
        body = f"[note](:/{identifier})\n`![example](:/{code_identifier})`\n"
        adapter._paged = lambda path, fields: iter([
            {"id": "source", "body": body}, {"id": identifier, "body": "target"},
        ])
        notes = adapter.list_notes()
        self.assertEqual(notes[0].native["attachment_issues"], [])
        self.assertEqual(notes[0].body, body)

    def test_markdown_hard_breaks_and_code_indentation_affect_signature(self):
        note = make_note(E.JOPLIN, native_id="j", body="first  \nsecond")
        self.assertNotEqual(note.content_signature, replace(note, body="first\nsecond").content_signature)
        note = replace(note, body="    code\n")
        self.assertNotEqual(note.content_signature, replace(note, body="code\n").content_signature)

    def test_changed_target_before_operation_is_not_overwritten(self):
        source = make_note(E.JOPLIN, native_id="j", global_id="g", body="new")
        target = make_note(E.OBSIDIAN, native_id="o", global_id="g", body="old")
        ja, oa = FakeAdapter(E.JOPLIN, [source]), FakeAdapter(E.OBSIDIAN, [target])
        state = MemoryState({"g": state_record(replace(source, body="old"), target)})
        engine = SyncEngine(self.config, adapters={E.JOPLIN: ja, E.OBSIDIAN: oa}, state_store=state)
        def progress(current, total, message):
            if message.startswith("正在处理"):
                oa.notes = [replace(target, body="concurrent edit", revision="2")]
        try:
            engine.execute(engine.preview(self.options), progress=progress)
        except SyncEngineError:
            pass
        self.assertEqual(oa.notes[0].body, "concurrent edit")
        self.assertEqual(oa.writes, [])

    def test_corrupt_state_does_not_silently_start_fresh(self):
        path = self.vault / "state.json"
        for invalid in ('{"groups":', '{"version": 1, "groups": {"g": {"endpoints": []}}}'):
            with self.subTest(invalid=invalid):
                path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ValueError):
                    StateStore(path).load()

    def test_partial_failure_records_successful_target_and_continues(self):
        source = make_note(E.JOPLIN, native_id="j", body="new")
        ja, oa, sa = FakeAdapter(E.JOPLIN, [source]), FakeAdapter(E.OBSIDIAN), FakeAdapter(E.SIYUAN)
        oa.upsert_note = lambda *args: (_ for _ in ()).throw(AdapterError("write failed"))
        engine = SyncEngine(self.config, adapters={E.JOPLIN: ja, E.OBSIDIAN: oa, E.SIYUAN: sa}, state_store=MemoryState())
        options = SyncOptions(mode=SyncMode.ONE_WAY, endpoints=tuple(E), source=E.JOPLIN)
        result = engine.execute(engine.preview(options))
        self.assertEqual({r.endpoint: r.success for r in result.targets}, {E.OBSIDIAN: False, E.SIYUAN: True})
        self.assertEqual(sa.notes[0].body, "new")

    def test_scan_cancellation_is_observed_between_notes(self):
        event = threading.Event()
        adapter = ObsidianAdapter(self.config)
        adapter.cancel_event = event
        event.set()
        with self.assertRaisesRegex(AdapterError, "取消"):
            adapter.list_notes()

    def test_unselected_row_is_not_written_or_baselined(self):
        source = make_note(E.JOPLIN, native_id="j")
        ja, oa = FakeAdapter(E.JOPLIN, [source]), FakeAdapter(E.OBSIDIAN)
        state = MemoryState()
        engine = SyncEngine(self.config, adapters={E.JOPLIN: ja, E.OBSIDIAN: oa}, state_store=state)
        plan = engine.preview(self.options)
        plan.operations[0].selected = False
        result = engine.execute(plan)
        self.assertEqual(result.completed, 0)
        self.assertEqual(oa.writes, [])
        self.assertEqual(state.groups, {})

    def test_target_edit_after_write_does_not_become_successful_baseline(self):
        source = make_note(E.JOPLIN, native_id="j", global_id="g", body="new")
        target = make_note(E.OBSIDIAN, native_id="o", global_id="g", body="old")
        ja, oa = FakeAdapter(E.JOPLIN, [source]), FakeAdapter(E.OBSIDIAN, [target])
        state = MemoryState({"g": state_record(replace(source, body="old"), target)})
        engine = SyncEngine(self.config, adapters={E.JOPLIN: ja, E.OBSIDIAN: oa}, state_store=state)
        write = oa.upsert_note
        def edit_after_write(*args):
            identifier = write(*args)
            oa.notes[0] = replace(oa.notes[0], body="target user edit")
            return identifier
        oa.upsert_note = edit_after_write
        result = engine.execute(engine.preview(self.options))
        self.assertTrue(result.errors)
        self.assertEqual(state.groups["g"]["endpoints"][E.OBSIDIAN.value]["signature"], target.content_signature)
        self.assertEqual(engine.preview(self.options).operations[0].action, OperationAction.CONFLICT)

    def test_checkpoint_survives_failure_of_later_note(self):
        sources = [make_note(E.JOPLIN, native_id=str(i), title=title) for i, title in enumerate(("A", "B"))]
        ja, oa = FakeAdapter(E.JOPLIN, sources), FakeAdapter(E.OBSIDIAN)
        state = MemoryState()
        engine = SyncEngine(self.config, adapters={E.JOPLIN: ja, E.OBSIDIAN: oa}, state_store=state)
        write = oa.upsert_note
        def fail_later(source, *args):
            if source.title == "B":
                raise AdapterError("disconnected")
            return write(source, *args)
        oa.upsert_note = fail_later
        result = engine.execute(engine.preview(self.options))
        self.assertEqual(result.completed, 1)
        saved = [group["endpoints"] for group in state.saved.values()
                 if group["endpoints"][E.JOPLIN.value]["native_id"] == sources[0].native_id]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][E.OBSIDIAN.value]["title"], "A")

    def test_colliding_normalized_titles_block_both_writes_in_preview(self):
        source = [make_note(E.JOPLIN, native_id=str(i), title=title) for i, title in enumerate(("A:B", "A?B"))]
        engine = SyncEngine(self.config, adapters={E.JOPLIN: FakeAdapter(E.JOPLIN, source), E.OBSIDIAN: ObsidianAdapter(self.config)}, state_store=MemoryState())
        plan = engine.preview(self.options)
        self.assertEqual(len(plan.operations), 2)
        self.assertEqual(plan.executable_operations(), [])

    def test_joplin_resource_replacement_leaves_code_examples_unchanged(self):
        from note_sync_hub.attachments import replace_joplin_resource_links
        identifier = "a" * 32
        example = f"`![example](:/{identifier})`"
        body = f"![real](:/{identifier})\n{example}"
        converted = replace_joplin_resource_links(body, {identifier: "new.png"})
        self.assertIn("![real](new.png)", converted)
        self.assertIn(example, converted)

    def test_write_verification_accepts_reused_asset_with_different_filename(self):
        from note_sync_hub.attachments import canonical_asset_uri
        digest = "a" * 64
        source = make_note(E.JOPLIN, native_id="j", body=f"![image]({canonical_asset_uri(digest, 'new.png')})")
        actual = replace(source, endpoint=E.OBSIDIAN, body=f"![image]({canonical_asset_uri(digest, 'old.png')})")
        self.assertTrue(ObsidianAdapter(self.config).matches_written(actual, source, "A"))
