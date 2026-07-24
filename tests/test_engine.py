from __future__ import annotations

import threading
import tempfile
import unittest
from dataclasses import replace

from note_sync_hub.adapters.base import AdapterError, NoteAdapter
from note_sync_hub.config import AppConfig
from note_sync_hub.engine import SyncEngine, SyncEngineError
from note_sync_hub.models import (
    ConflictPolicy,
    Endpoint,
    Note,
    OperationAction,
    SyncMode,
    SyncOptions,
    TargetMode,
)


def make_note(
    endpoint: Endpoint,
    *,
    native_id: str,
    title: str = "笔记",
    folder: str = "A",
    body: str = "正文\n",
    global_id: str = "",
    revision: str = "1",
    updated: int = 1,
) -> Note:
    return Note(
        endpoint=endpoint,
        native_id=native_id,
        global_id=global_id,
        title=title,
        folder=folder,
        body=body,
        revision=revision,
        updated=updated,
        locator=native_id,
    )


class MemoryState:
    def __init__(self, groups=None):
        self.groups = groups or {}
        self.saved = None
        self.backups = 0

    def load(self):
        return {"version": 1, "groups": self.groups}

    def save(self, groups):
        self.saved = groups
        self.groups = groups

    def backup(self):
        self.backups += 1
        return None


class FakeAdapter(NoteAdapter):
    def __init__(self, endpoint: Endpoint, notes=None):
        self.endpoint = endpoint
        self.notes = list(notes or [])
        self.writes = []
        self.links = []
        self.deleted = []
        self._counter = 0

    def test_connection(self):
        return f"{self.endpoint.label} ok"

    def list_folders(self):
        return sorted({"", *(note.folder for note in self.notes)})

    def list_notes(self):
        return list(self.notes)

    def upsert_note(self, source, existing, folder, global_id):
        self._counter += 1
        native_id = existing.native_id if existing else f"{self.endpoint.value}-{self._counter}"
        written = Note(
            endpoint=self.endpoint,
            native_id=native_id,
            global_id=global_id,
            title=source.title,
            folder=folder,
            body=source.body,
            tags=source.tags,
            revision=f"write-{self._counter}",
            updated=source.updated + self._counter,
            locator=native_id,
            assets=source.assets,
        )
        if existing:
            self.notes = [written if note.native_id == existing.native_id else note for note in self.notes]
        else:
            self.notes.append(written)
        self.writes.append((source.endpoint, native_id, folder, global_id))
        return native_id

    def set_global_id(self, note, global_id):
        note.global_id = global_id
        note.revision += "-linked"
        note.native.pop("metadata_needs_repair", None)
        self.links.append((note.native_id, global_id))

    def move_to_trash(self, note):
        self.notes = [item for item in self.notes if item.native_id != note.native_id]
        self.deleted.append(note.native_id)


def state_record(*notes: Note):
    endpoints = {}
    for note in notes:
        endpoints[note.endpoint.value] = {
            "native_id": note.native_id,
            "title": note.title,
            "folder": note.folder,
            "signature": note.content_signature,
            "revision": note.revision,
            "updated": note.updated,
            "locator": note.locator,
        }
    return {"endpoints": endpoints}


class NormalizingFakeAdapter(FakeAdapter):
    def upsert_note(self, source, existing, folder, global_id):
        native_id = super().upsert_note(source, existing, folder, global_id)
        written = next(note for note in self.notes if note.native_id == native_id)
        normalized = replace(written, body=f"# {written.title}\n\n{written.body}")
        self.notes = [normalized if note.native_id == native_id else note for note in self.notes]
        return native_id


class EnginePlannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = AppConfig(
            joplin_token="test",
            obsidian_vault_path=self.temporary.name,
            siyuan_token="test",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def engine(self, by_endpoint, groups=None):
        adapters = {
            endpoint: FakeAdapter(endpoint, by_endpoint.get(endpoint, []))
            for endpoint in Endpoint
        }
        state = MemoryState(groups)
        engine = SyncEngine(self.config, adapters=adapters, state_store=state)
        return engine, adapters, state

    def test_one_way_source_can_create_both_targets(self):
        source = make_note(Endpoint.JOPLIN, native_id="j1")
        engine, _adapters, _state = self.engine({Endpoint.JOPLIN: [source]})
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.JOPLIN,
        )
        plan = engine.preview(options)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].action, OperationAction.CREATE)
        self.assertEqual(set(plan.operations[0].targets), {Endpoint.OBSIDIAN, Endpoint.SIYUAN})

    def test_one_way_selected_folder_mapping_applies_per_target(self):
        source = make_note(Endpoint.JOPLIN, native_id="j1", folder="来源/A/子目录")
        engine, _adapters, _state = self.engine({Endpoint.JOPLIN: [source]})
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.JOPLIN,
            scope_all=False,
            selected_folders={Endpoint.JOPLIN: ("来源/A",)},
            target_mode=TargetMode.SELECTED,
            target_folders={Endpoint.OBSIDIAN: "B", Endpoint.SIYUAN: "知识库"},
        )
        operation = engine.preview(options).operations[0]
        self.assertEqual(operation.target_folders[Endpoint.OBSIDIAN], "B/A/子目录")
        self.assertEqual(operation.target_folders[Endpoint.SIYUAN], "知识库/A/子目录")

    def test_selecting_siyuan_document_includes_document_and_children(self):
        parent = make_note(Endpoint.SIYUAN, native_id="s1", title="项目", folder="知识库")
        child = make_note(Endpoint.SIYUAN, native_id="s2", title="记录", folder="知识库/项目")
        engine, _adapters, _state = self.engine({Endpoint.SIYUAN: [parent, child]})
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.SIYUAN, Endpoint.OBSIDIAN),
            source=Endpoint.SIYUAN,
            scope_all=False,
            selected_folders={Endpoint.SIYUAN: ("知识库/项目",)},
            target_mode=TargetMode.SELECTED,
            target_folders={Endpoint.OBSIDIAN: "B"},
        )
        operations = engine.preview(options).operations
        self.assertEqual({item.title for item in operations}, {"项目", "记录"})
        folders = {item.title: item.target_folders[Endpoint.OBSIDIAN] for item in operations}
        self.assertEqual(folders, {"项目": "B", "记录": "B/项目"})

    def test_first_run_different_versions_are_conflict(self):
        left = make_note(Endpoint.JOPLIN, native_id="j1", body="J\n")
        right = make_note(Endpoint.OBSIDIAN, native_id="o1", body="O\n")
        engine, _adapters, _state = self.engine({Endpoint.JOPLIN: [left], Endpoint.OBSIDIAN: [right]})
        options = SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
        )
        self.assertEqual(engine.preview(options).operations[0].action, OperationAction.CONFLICT)

    def test_first_run_latest_policy_selects_unique_newer_version(self):
        left = make_note(Endpoint.JOPLIN, native_id="j1", body="J\n", updated=1_700_000_000_000)
        right = make_note(Endpoint.OBSIDIAN, native_id="o1", body="O\n", updated=1_700_000_001_000)
        engine, _adapters, _state = self.engine({Endpoint.JOPLIN: [left], Endpoint.OBSIDIAN: [right]})

        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            conflict_policy=ConflictPolicy.LATEST,
        )).operations[0]

        self.assertEqual(operation.action, OperationAction.UPDATE)
        self.assertEqual(operation.source, Endpoint.OBSIDIAN)
        self.assertEqual(operation.targets, (Endpoint.JOPLIN,))
        self.assertIn("自动采用最新版本", operation.reason)

    def test_one_changed_endpoint_propagates_to_other_two(self):
        global_id = "group-1"
        old = [
            make_note(endpoint, native_id=endpoint.value[0], global_id=global_id)
            for endpoint in Endpoint
        ]
        current = [replace(note, body="新正文\n", revision="2", updated=2) if note.endpoint == Endpoint.JOPLIN else note for note in old]
        engine, _adapters, _state = self.engine(
            {note.endpoint: [note] for note in current},
            {global_id: state_record(*old)},
        )
        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=tuple(Endpoint),
        )).operations[0]
        self.assertEqual(operation.source, Endpoint.JOPLIN)
        self.assertEqual(set(operation.targets), {Endpoint.OBSIDIAN, Endpoint.SIYUAN})

    def test_two_different_changes_are_conflict(self):
        global_id = "group-2"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        new_j = replace(old_j, body="J 改\n", revision="2")
        new_o = replace(old_o, body="O 改\n", revision="2")
        engine, _adapters, _state = self.engine(
            {Endpoint.JOPLIN: [new_j], Endpoint.OBSIDIAN: [new_o]},
            {global_id: state_record(old_j, old_o)},
        )
        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
        )).operations[0]
        self.assertEqual(operation.action, OperationAction.CONFLICT)

    def test_two_different_changes_latest_policy_selects_newer_endpoint(self):
        global_id = "latest-wins"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        new_j = replace(old_j, body="J 改\n", revision="2", updated=1_700_000_001_000)
        new_o = replace(old_o, body="O 改\n", revision="2", updated=1_700_000_002_000)
        engine, adapters, _state = self.engine(
            {Endpoint.JOPLIN: [new_j], Endpoint.OBSIDIAN: [new_o]},
            {global_id: state_record(old_j, old_o)},
        )

        plan = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            primary=Endpoint.JOPLIN,
            conflict_policy=ConflictPolicy.LATEST,
        ))
        operation = plan.operations[0]

        self.assertEqual(operation.action, OperationAction.UPDATE)
        self.assertEqual(operation.source, Endpoint.OBSIDIAN)
        self.assertEqual(operation.targets, (Endpoint.JOPLIN,))
        self.assertEqual(engine.execute(plan).errors, [])
        self.assertEqual(adapters[Endpoint.JOPLIN].notes[0].body, "O 改\n")

    def test_latest_policy_keeps_equal_timestamps_as_conflict(self):
        global_id = "latest-tie"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        new_j = replace(old_j, body="J 改\n", revision="2", updated=1_700_000_001_000)
        new_o = replace(old_o, body="O 改\n", revision="2", updated=1_700_000_001_000)
        engine, _adapters, _state = self.engine(
            {Endpoint.JOPLIN: [new_j], Endpoint.OBSIDIAN: [new_o]},
            {global_id: state_record(old_j, old_o)},
        )

        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            conflict_policy=ConflictPolicy.LATEST,
        )).operations[0]

        self.assertEqual(operation.action, OperationAction.CONFLICT)
        self.assertIn("修改时间相同", operation.reason)

    def test_latest_policy_rejects_three_way_sync(self):
        options = SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=tuple(Endpoint),
            conflict_policy=ConflictPolicy.LATEST,
        )
        with self.assertRaisesRegex(ValueError, "仅适用于 Joplin 与 Obsidian"):
            options.validate()

    def test_duplicate_global_id_blocks_the_entire_group(self):
        global_id = "duplicate"
        first = make_note(Endpoint.JOPLIN, native_id="j1", global_id=global_id)
        second = make_note(Endpoint.JOPLIN, native_id="j2", global_id=global_id)
        other = make_note(Endpoint.OBSIDIAN, native_id="o1", global_id=global_id)
        engine, _adapters, _state = self.engine({
            Endpoint.JOPLIN: [first, second],
            Endpoint.OBSIDIAN: [other],
        })
        plan = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
        ))
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].action, OperationAction.CONFLICT)
        self.assertIn("重复", plan.operations[0].reason)

    def test_one_way_duplicate_target_id_uses_unique_expected_path(self):
        global_id = "duplicate-target"
        source = make_note(
            Endpoint.OBSIDIAN,
            native_id="o1",
            global_id=global_id,
            folder="当前目录",
            body="来源新正文\n",
        )
        expected = make_note(
            Endpoint.SIYUAN,
            native_id="s-current",
            global_id=global_id,
            folder="当前目录",
            body="旧正文\n",
        )
        stale = make_note(
            Endpoint.SIYUAN,
            native_id="s-stale",
            global_id=global_id,
            folder="以前目录",
            body="旧正文\n",
        )
        engine, _adapters, _state = self.engine({
            Endpoint.OBSIDIAN: [source],
            Endpoint.SIYUAN: [expected, stale],
        })

        plan = engine.preview(SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        ))

        self.assertEqual([operation.action for operation in plan.operations], [OperationAction.UPDATE])
        self.assertEqual(plan.operations[0].versions[Endpoint.SIYUAN].native_id, "s-current")

    def test_one_way_duplicate_target_with_stale_baseline_converges(self):
        global_id = "duplicate-target-stale-baseline"
        source = make_note(
            Endpoint.OBSIDIAN,
            native_id="o1",
            global_id=global_id,
            folder="当前目录",
            body="来源新正文\n",
        )
        expected = make_note(
            Endpoint.SIYUAN,
            native_id="s-current",
            global_id=global_id,
            folder="当前目录",
            body="旧正文\n",
        )
        stale = make_note(
            Endpoint.SIYUAN,
            native_id="s-stale",
            global_id=global_id,
            folder="以前目录",
            body="历史副本\n",
        )
        adapters = {
            Endpoint.OBSIDIAN: FakeAdapter(Endpoint.OBSIDIAN, [source]),
            Endpoint.SIYUAN: NormalizingFakeAdapter(Endpoint.SIYUAN, [expected, stale]),
        }
        state = MemoryState({global_id: state_record(source, stale)})
        engine = SyncEngine(self.config, adapters=adapters, state_store=state)
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        )

        first_plan = engine.preview(options)

        self.assertEqual(
            [operation.action for operation in first_plan.operations],
            [OperationAction.UPDATE],
        )
        self.assertEqual(
            first_plan.operations[0].versions[Endpoint.SIYUAN].native_id,
            "s-current",
        )
        self.assertEqual(engine.execute(first_plan).errors, [])
        self.assertEqual(
            state.saved[global_id]["endpoints"][Endpoint.SIYUAN.value]["native_id"],
            "s-current",
        )
        self.assertEqual(
            next(note for note in adapters[Endpoint.SIYUAN].notes if note.native_id == "s-stale").body,
            "历史副本\n",
        )
        self.assertEqual(engine.preview(options).operations, [])

    def test_one_way_duplicate_target_id_without_unique_expected_path_is_conflict(self):
        global_id = "ambiguous-duplicate-target"
        source = make_note(
            Endpoint.OBSIDIAN,
            native_id="o1",
            global_id=global_id,
            folder="当前目录",
        )
        first = make_note(
            Endpoint.SIYUAN,
            native_id="s1",
            global_id=global_id,
            folder="以前目录一",
        )
        second = make_note(
            Endpoint.SIYUAN,
            native_id="s2",
            global_id=global_id,
            folder="以前目录二",
        )
        engine, _adapters, _state = self.engine({
            Endpoint.OBSIDIAN: [source],
            Endpoint.SIYUAN: [first, second],
        })

        plan = engine.preview(SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        ))

        self.assertEqual([operation.action for operation in plan.operations], [OperationAction.CONFLICT])
        self.assertIn("重复", plan.operations[0].reason)

    def test_same_simultaneous_change_updates_baseline_without_overwrite(self):
        global_id = "same-change"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", title="旧标题", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o", title="旧标题", global_id=global_id)
        new_j = replace(old_j, title="新标题", body="相同修改\n", revision="2", updated=2)
        new_o = replace(old_o, title="新标题", body="相同修改\n", revision="2", updated=2)
        engine, _adapters, _state = self.engine(
            {Endpoint.JOPLIN: [new_j], Endpoint.OBSIDIAN: [new_o]},
            {global_id: state_record(old_j, old_o)},
        )
        options = SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
        )
        plan = engine.preview(options)
        self.assertEqual(plan.operations[0].action, OperationAction.LINK)
        self.assertEqual(engine.execute(plan).errors, [])
        self.assertEqual(engine.preview(options).operations, [])

    def test_deleted_source_is_retained_by_default(self):
        global_id = "group-3"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        current_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        engine, _adapters, _state = self.engine(
            {Endpoint.OBSIDIAN: [current_o]},
            {global_id: state_record(old_j, current_o)},
        )
        operation = engine.preview(SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            source=Endpoint.JOPLIN,
        )).operations[0]
        self.assertEqual(operation.action, OperationAction.SKIP)

    def test_delete_propagation_includes_siyuan_managed_trash(self):
        global_id = "group-4"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        current_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        current_s = make_note(Endpoint.SIYUAN, native_id="s", global_id=global_id)
        engine, adapters, _state = self.engine(
            {Endpoint.OBSIDIAN: [current_o], Endpoint.SIYUAN: [current_s]},
            {global_id: state_record(old_j, current_o, current_s)},
        )
        plan = engine.preview(SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=tuple(Endpoint),
            source=Endpoint.JOPLIN,
            propagate_deletions=True,
        ))
        operations = plan.operations
        self.assertEqual([item.action for item in operations], [OperationAction.DELETE])
        self.assertEqual(operations[0].targets, (Endpoint.OBSIDIAN, Endpoint.SIYUAN))
        self.assertIn("思源移入", operations[0].reason)
        self.assertEqual(engine.execute(plan).errors, [])
        self.assertEqual(adapters[Endpoint.OBSIDIAN].deleted, ["o"])
        self.assertEqual(adapters[Endpoint.SIYUAN].deleted, ["s"])

    def test_bidirectional_non_primary_deletion_is_restored_from_primary(self):
        global_id = "restore-secondary"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        engine, _adapters, _state = self.engine(
            {Endpoint.OBSIDIAN: [old_o]},
            {global_id: state_record(old_j, old_o)},
        )
        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            primary=Endpoint.OBSIDIAN,
            propagate_deletions=True,
        )).operations[0]
        self.assertEqual(operation.action, OperationAction.CREATE)
        self.assertEqual(operation.source, Endpoint.OBSIDIAN)
        self.assertEqual(operation.targets, (Endpoint.JOPLIN,))
        self.assertIn("非主端", operation.reason)

    def test_bidirectional_primary_deletion_can_propagate(self):
        global_id = "delete-from-primary"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        current_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        current_s = make_note(Endpoint.SIYUAN, native_id="s", global_id=global_id)
        engine, _adapters, _state = self.engine(
            {Endpoint.OBSIDIAN: [current_o], Endpoint.SIYUAN: [current_s]},
            {global_id: state_record(old_j, current_o, current_s)},
        )
        operations = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=tuple(Endpoint),
            primary=Endpoint.JOPLIN,
            propagate_deletions=True,
        )).operations
        self.assertEqual([item.action for item in operations], [OperationAction.DELETE])
        self.assertEqual(operations[0].targets, (Endpoint.OBSIDIAN, Endpoint.SIYUAN))
        self.assertIn("主端 Joplin", operations[0].reason)

    def test_bidirectional_primary_deletion_is_retained_when_delete_is_off(self):
        global_id = "keep-after-primary-delete"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        current_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        engine, _adapters, _state = self.engine(
            {Endpoint.OBSIDIAN: [current_o]},
            {global_id: state_record(old_j, current_o)},
        )
        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            primary=Endpoint.JOPLIN,
        )).operations[0]
        self.assertEqual(operation.action, OperationAction.SKIP)
        self.assertIn("主端 Joplin", operation.reason)

    def test_non_primary_edit_propagates_while_another_secondary_is_restored(self):
        global_id = "edit-and-restore"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        old_s = make_note(Endpoint.SIYUAN, native_id="s", global_id=global_id)
        changed_o = replace(old_o, body="Obsidian 修改\n", revision="2", updated=2)
        engine, _adapters, _state = self.engine(
            {Endpoint.JOPLIN: [old_j], Endpoint.OBSIDIAN: [changed_o]},
            {global_id: state_record(old_j, old_o, old_s)},
        )
        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=tuple(Endpoint),
            primary=Endpoint.JOPLIN,
        )).operations[0]
        self.assertEqual(operation.source, Endpoint.OBSIDIAN)
        self.assertEqual(set(operation.targets), {Endpoint.JOPLIN, Endpoint.SIYUAN})
        self.assertIn("非主端删除", operation.reason)

    def test_primary_deletion_plus_secondary_edit_remains_conflict(self):
        global_id = "delete-edit-conflict"
        old_j = make_note(Endpoint.JOPLIN, native_id="j", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o", global_id=global_id)
        changed_o = replace(old_o, body="仍需保留的修改\n", revision="2")
        engine, _adapters, _state = self.engine(
            {Endpoint.OBSIDIAN: [changed_o]},
            {global_id: state_record(old_j, old_o)},
        )
        operation = engine.preview(SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            primary=Endpoint.JOPLIN,
            propagate_deletions=True,
            conflict_policy=ConflictPolicy.LATEST,
        )).operations[0]
        self.assertEqual(operation.action, OperationAction.CONFLICT)
        self.assertIn("主端 Joplin 已删除", operation.reason)

    def test_attachment_issue_blocks_source_write(self):
        source = make_note(Endpoint.OBSIDIAN, native_id="o1")
        source.native["attachment_issues"] = ["找不到附件 image.png"]
        engine, _adapters, _state = self.engine({Endpoint.OBSIDIAN: [source]})
        operation = engine.preview(SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.JOPLIN),
            source=Endpoint.OBSIDIAN,
        )).operations[0]
        self.assertEqual(operation.action, OperationAction.CONFLICT)

    def test_execute_creates_targets_links_source_and_saves_state(self):
        source = make_note(Endpoint.JOPLIN, native_id="j1")
        engine, adapters, state = self.engine({Endpoint.JOPLIN: [source]})
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=tuple(Endpoint),
            source=Endpoint.JOPLIN,
        )
        result = engine.execute(engine.preview(options))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.completed, 1)
        self.assertEqual(len(adapters[Endpoint.OBSIDIAN].writes), 1)
        self.assertEqual(len(adapters[Endpoint.SIYUAN].writes), 1)
        self.assertTrue(source.global_id)
        self.assertIn(source.global_id, state.saved)

    def test_one_way_success_converges_when_target_normalizes_written_content(self):
        source = make_note(Endpoint.OBSIDIAN, native_id="o1", title="规范化", body="正文\n")
        adapters = {
            Endpoint.JOPLIN: FakeAdapter(Endpoint.JOPLIN),
            Endpoint.OBSIDIAN: FakeAdapter(Endpoint.OBSIDIAN, [source]),
            Endpoint.SIYUAN: NormalizingFakeAdapter(Endpoint.SIYUAN),
        }
        state = MemoryState()
        engine = SyncEngine(self.config, adapters=adapters, state_store=state)
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        )

        result = engine.execute(engine.preview(options))

        self.assertEqual(result.errors, [])
        self.assertEqual(engine.preview(options).operations, [])

    def test_one_way_siyuan_title_normalization_does_not_repeat_update(self):
        long_title = "长" * 129
        global_id = "normalized-title"
        source = make_note(
            Endpoint.OBSIDIAN, native_id="o1", global_id=global_id, title=long_title, body="正文\n"
        )
        target = make_note(
            Endpoint.SIYUAN, native_id="s1", global_id=global_id, title="长" * 128, body="正文\n"
        )
        adapters = {
            Endpoint.OBSIDIAN: FakeAdapter(Endpoint.OBSIDIAN, [source]),
            Endpoint.SIYUAN: FakeAdapter(Endpoint.SIYUAN, [target]),
        }
        adapters[Endpoint.SIYUAN].normalize_target_title = lambda title: title[:128]
        engine = SyncEngine(self.config, adapters=adapters, state_store=MemoryState({
            global_id: state_record(source, target),
        }))
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        )

        self.assertEqual(engine.preview(options).operations, [])

    def test_one_way_updates_when_source_changes_after_normalized_baseline(self):
        global_id = "normalized-baseline"
        old_source = make_note(
            Endpoint.OBSIDIAN, native_id="o1", global_id=global_id, title="规范化", body="旧正文\n"
        )
        old_target = make_note(
            Endpoint.SIYUAN, native_id="s1", global_id=global_id, title="规范化",
            body="# 规范化\n\n旧正文\n",
        )
        changed_source = replace(old_source, body="新正文\n", revision="2")
        engine, _adapters, _state = self.engine(
            {Endpoint.OBSIDIAN: [changed_source], Endpoint.SIYUAN: [old_target]},
            {global_id: state_record(old_source, old_target)},
        )
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        )

        operation = engine.preview(options).operations[0]

        self.assertEqual(operation.action, OperationAction.UPDATE)
        self.assertEqual(operation.targets, (Endpoint.SIYUAN,))

    def test_one_way_relinks_unmarked_target_when_source_already_has_global_id(self):
        global_id = "partial-failure"
        source = make_note(
            Endpoint.OBSIDIAN, native_id="o1", global_id=global_id, title="已有文档", folder="A"
        )
        target = make_note(
            Endpoint.SIYUAN, native_id="s1", global_id="", title="已有文档", folder="A"
        )
        engine, _adapters, _state = self.engine({Endpoint.OBSIDIAN: [source], Endpoint.SIYUAN: [target]})
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        )

        operation = engine.preview(options).operations[0]

        self.assertEqual(operation.action, OperationAction.LINK)
        self.assertEqual(operation.global_id, global_id)
        self.assertEqual(operation.versions[Endpoint.SIYUAN].native_id, "s1")

    def test_one_way_relinks_same_path_target_with_orphaned_old_global_id(self):
        source = make_note(
            Endpoint.OBSIDIAN,
            native_id="o1",
            global_id="current-group",
            title="已有周报",
            folder="工作台/周报",
        )
        target = make_note(
            Endpoint.JOPLIN,
            native_id="j1",
            global_id="orphaned-old-group",
            title="已有周报",
            folder="工作台/周报",
        )
        engine, adapters, state = self.engine(
            {
                Endpoint.OBSIDIAN: [source],
                Endpoint.JOPLIN: [target],
            },
            {
                "current-group": state_record(source),
                "orphaned-old-group": state_record(target),
            },
        )
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.JOPLIN),
            source=Endpoint.OBSIDIAN,
        )

        plan = engine.preview(options)

        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].action, OperationAction.LINK)
        self.assertEqual(plan.operations[0].global_id, "current-group")
        self.assertEqual(plan.operations[0].versions[Endpoint.JOPLIN].native_id, "j1")
        self.assertEqual(engine.execute(plan).errors, [])
        self.assertEqual(adapters[Endpoint.JOPLIN].notes[0].global_id, "current-group")
        self.assertNotIn("orphaned-old-group", state.saved)
        self.assertEqual(engine.preview(options).operations, [])

    def test_one_way_repairs_malformed_source_metadata_and_converges(self):
        global_id = "recoverable-source-id"
        source = make_note(
            Endpoint.OBSIDIAN,
            native_id="o1",
            global_id=global_id,
            title="已有笔记",
        )
        source.native["metadata_needs_repair"] = True
        target = make_note(
            Endpoint.JOPLIN,
            native_id="j1",
            global_id=global_id,
            title="已有笔记",
        )
        engine, adapters, _state = self.engine({
            Endpoint.OBSIDIAN: [source],
            Endpoint.JOPLIN: [target],
        })
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.JOPLIN),
            source=Endpoint.OBSIDIAN,
        )

        plan = engine.preview(options)

        self.assertEqual([operation.action for operation in plan.operations], [OperationAction.LINK])
        self.assertEqual(engine.execute(plan).errors, [])
        self.assertEqual(adapters[Endpoint.OBSIDIAN].links, [("o1", global_id)])
        self.assertEqual(engine.preview(options).operations, [])

    def test_pair_sync_preserves_unselected_endpoint_baseline(self):
        global_id = "three-endpoint-baseline"
        old_j = make_note(Endpoint.JOPLIN, native_id="j1", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o1", global_id=global_id)
        old_s = make_note(Endpoint.SIYUAN, native_id="s1", global_id=global_id)
        changed_o = replace(old_o, body="Obsidian 新正文\n", revision="2", updated=2)
        engine, _adapters, state = self.engine(
            {
                Endpoint.JOPLIN: [old_j],
                Endpoint.OBSIDIAN: [changed_o],
                Endpoint.SIYUAN: [old_s],
            },
            {global_id: state_record(old_j, old_o, old_s)},
        )
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN,
        )

        result = engine.execute(engine.preview(options))

        self.assertEqual(result.errors, [])
        self.assertEqual(
            set(state.saved[global_id]["endpoints"]),
            {Endpoint.JOPLIN.value, Endpoint.OBSIDIAN.value, Endpoint.SIYUAN.value},
        )
        self.assertEqual(
            state.saved[global_id]["endpoints"][Endpoint.JOPLIN.value]["signature"],
            old_j.content_signature,
        )

    def test_execute_rejects_a_stale_preview(self):
        source = make_note(Endpoint.JOPLIN, native_id="j1")
        engine, adapters, _state = self.engine({Endpoint.JOPLIN: [source]})
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            source=Endpoint.JOPLIN,
        )
        plan = engine.preview(options)
        adapters[Endpoint.JOPLIN].notes[0] = replace(source, body="预览后变化\n", revision="2")
        with self.assertRaises(SyncEngineError):
            engine.execute(plan)

    def test_resolved_conflict_does_not_restore_stale_source_after_writing_merge(self):
        left = make_note(Endpoint.JOPLIN, native_id="j1", body="J\n")
        right = make_note(Endpoint.OBSIDIAN, native_id="o1", body="O\n")
        engine, adapters, _state = self.engine({Endpoint.JOPLIN: [left], Endpoint.OBSIDIAN: [right]})
        options = SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
        )
        plan = engine.preview(options)
        operation = plan.operations[0]
        operation.resolved_note = replace(left, body="合并结果\n")
        operation.source = Endpoint.JOPLIN
        operation.targets = options.endpoints
        operation.target_folders = {endpoint: "A" for endpoint in options.endpoints}
        operation.action = OperationAction.UPDATE
        result = engine.execute(plan)
        self.assertEqual(result.errors, [])
        self.assertEqual(adapters[Endpoint.JOPLIN].notes[0].body, "合并结果\n")
        self.assertEqual(adapters[Endpoint.OBSIDIAN].notes[0].body, "合并结果\n")
        self.assertEqual(adapters[Endpoint.JOPLIN].links, [])

    def test_partial_multi_target_failure_keeps_successful_copies_associated(self):
        source = make_note(Endpoint.JOPLIN, native_id="j1")
        engine, adapters, state = self.engine({Endpoint.JOPLIN: [source]})

        def fail_write(*_args, **_kwargs):
            raise AdapterError("模拟思源写入失败")

        adapters[Endpoint.SIYUAN].upsert_note = fail_write
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=tuple(Endpoint),
            source=Endpoint.JOPLIN,
        )
        result = engine.execute(engine.preview(options))
        self.assertEqual(result.completed, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(source.global_id)
        self.assertEqual(len(adapters[Endpoint.OBSIDIAN].notes), 1)
        self.assertIn(source.global_id, state.saved)
        self.assertEqual(
            set(state.saved[source.global_id]["endpoints"]),
            {Endpoint.JOPLIN.value, Endpoint.OBSIDIAN.value},
        )

    def test_executing_safe_rows_does_not_baseline_an_unresolved_conflict(self):
        global_id = "conflict-group"
        old_j = make_note(Endpoint.JOPLIN, native_id="j-conflict", title="冲突", body="旧\n", global_id=global_id)
        old_o = make_note(Endpoint.OBSIDIAN, native_id="o-conflict", title="冲突", body="旧\n", global_id=global_id)
        new_j = replace(old_j, body="J 修改\n", revision="2")
        new_o = replace(old_o, body="O 修改\n", revision="2")
        new_note = make_note(Endpoint.JOPLIN, native_id="j-new", title="安全新建", body="新\n")
        engine, _adapters, state = self.engine(
            {Endpoint.JOPLIN: [new_j, new_note], Endpoint.OBSIDIAN: [new_o]},
            {global_id: state_record(old_j, old_o)},
        )
        options = SyncOptions(
            mode=SyncMode.BIDIRECTIONAL,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
        )
        first_plan = engine.preview(options)
        self.assertEqual(
            {operation.title: operation.action for operation in first_plan.operations},
            {"冲突": OperationAction.CONFLICT, "安全新建": OperationAction.CREATE},
        )
        result = engine.execute(first_plan)
        self.assertEqual(result.errors, [])
        self.assertEqual(
            state.groups[global_id]["endpoints"][Endpoint.JOPLIN.value]["signature"],
            old_j.content_signature,
        )
        second_plan = engine.preview(options)
        conflict = next(operation for operation in second_plan.operations if operation.title == "冲突")
        self.assertEqual(conflict.action, OperationAction.CONFLICT)

    def test_cancel_before_first_operation_writes_nothing(self):
        source = make_note(Endpoint.JOPLIN, native_id="j1")
        engine, adapters, _state = self.engine({Endpoint.JOPLIN: [source]})
        options = SyncOptions(
            mode=SyncMode.ONE_WAY,
            endpoints=(Endpoint.JOPLIN, Endpoint.OBSIDIAN),
            source=Endpoint.JOPLIN,
        )
        event = threading.Event()
        event.set()
        result = engine.execute(engine.preview(options), cancel_event=event)
        self.assertEqual(result.completed, 0)
        self.assertEqual(adapters[Endpoint.OBSIDIAN].writes, [])


if __name__ == "__main__":
    unittest.main()
