from __future__ import annotations

import unittest
from types import SimpleNamespace

from note_sync_hub.gui import MODE_LABELS, TARGET_MODE_LABELS, SyncApp
from note_sync_hub.models import Endpoint, SyncMode, TargetMode


class StubVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value


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


if __name__ == "__main__":
    unittest.main()
