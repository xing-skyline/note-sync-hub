from __future__ import annotations

import unittest
from types import SimpleNamespace

from note_sync_hub.gui import (
    CONFLICT_POLICY_LABELS,
    MODE_LABELS,
    TARGET_MODE_LABELS,
    SyncApp,
)
from note_sync_hub.models import ConflictPolicy, Endpoint, SyncMode, TargetMode


class StubVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


class StubPackWidget:
    def __init__(self, manager="pack"):
        self.manager = manager
        self.pack_options = None

    def winfo_manager(self):
        return self.manager

    def pack_forget(self):
        self.manager = ""

    def pack(self, **options):
        self.manager = "pack"
        self.pack_options = options


class StubButton:
    def __init__(self):
        self.text = ""

    def configure(self, **options):
        self.text = options.get("text", self.text)


def label_for(mapping, value):
    return next(label for label, mapped_value in mapping.items() if mapped_value == value)


class SyncOptionCollectionTests(unittest.TestCase):
    def make_app(self, *, scope: str, target_mode: TargetMode):
        endpoints = (Endpoint.OBSIDIAN, Endpoint.SIYUAN)
        selections = {
            Endpoint.OBSIDIAN: ("来源文件夹",),
            Endpoint.SIYUAN: (),
        }
        return SimpleNamespace(
            _sync_endpoints=lambda: endpoints,
            _selected_folders=lambda endpoint: selections[endpoint],
            mode_var=StubVar(label_for(MODE_LABELS, SyncMode.ONE_WAY)),
            source_var=StubVar(Endpoint.OBSIDIAN.label),
            primary_var=StubVar(Endpoint.OBSIDIAN.label),
            conflict_policy_var=StubVar(label_for(CONFLICT_POLICY_LABELS, ConflictPolicy.MANUAL)),
            scope_var=StubVar(scope),
            include_subfolders_var=StubVar(True),
            target_mode_var=StubVar(label_for(TARGET_MODE_LABELS, target_mode)),
            target_folder_vars={
                Endpoint.OBSIDIAN: StubVar("旧来源目录值"),
                Endpoint.SIYUAN: StubVar("思源目标目录"),
            },
            propagate_deletions_var=StubVar(False),
        )

    def test_all_notes_ignores_stale_selected_folder_mapping(self):
        app = self.make_app(scope="all", target_mode=TargetMode.SELECTED)

        options = SyncApp._collect_options(app)

        self.assertTrue(options.scope_all)
        self.assertEqual(options.target_mode, TargetMode.PRESERVE)
        self.assertEqual(options.target_folders, {})

    def test_selected_folder_keeps_active_target_mapping(self):
        app = self.make_app(scope="selected", target_mode=TargetMode.SELECTED)

        options = SyncApp._collect_options(app)

        self.assertFalse(options.scope_all)
        self.assertEqual(options.target_mode, TargetMode.SELECTED)
        self.assertEqual(options.target_folders, {Endpoint.SIYUAN: "思源目标目录"})

    def test_joplin_obsidian_bidirectional_collects_latest_policy(self):
        endpoints = (Endpoint.JOPLIN, Endpoint.OBSIDIAN)
        app = SimpleNamespace(
            _sync_endpoints=lambda: endpoints,
            _selected_folders=lambda _endpoint: (),
            mode_var=StubVar(label_for(MODE_LABELS, SyncMode.BIDIRECTIONAL)),
            source_var=StubVar(Endpoint.JOPLIN.label),
            primary_var=StubVar(Endpoint.JOPLIN.label),
            conflict_policy_var=StubVar(label_for(CONFLICT_POLICY_LABELS, ConflictPolicy.LATEST)),
            scope_var=StubVar("all"),
            include_subfolders_var=StubVar(True),
            target_mode_var=StubVar(label_for(TARGET_MODE_LABELS, TargetMode.PRESERVE)),
            target_folder_vars={endpoint: StubVar("") for endpoint in endpoints},
            propagate_deletions_var=StubVar(False),
        )

        options = SyncApp._collect_options(app)

        self.assertEqual(options.conflict_policy, ConflictPolicy.LATEST)

    def test_preview_can_collapse_and_restore_folder_panel(self):
        panes = StubPackWidget()
        actions = StubPackWidget()
        button = StubButton()
        app = SimpleNamespace(
            folders_collapsed=False,
            folder_panes=panes,
            folder_actions=actions,
            folder_toggle_button=button,
        )

        SyncApp._set_folder_panel_collapsed(app, True)
        self.assertEqual(panes.winfo_manager(), "")
        self.assertEqual(button.text, "展开目录区")

        SyncApp._set_folder_panel_collapsed(app, False)
        self.assertEqual(panes.winfo_manager(), "pack")
        self.assertEqual(panes.pack_options["before"], actions)
        self.assertEqual(button.text, "收起目录区")


if __name__ == "__main__":
    unittest.main()
