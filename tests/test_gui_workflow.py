import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from note_sync_hub.config import AppConfig
from note_sync_hub.gui import SyncApp
from note_sync_hub.models import Endpoint as E, OperationAction as A, SyncMode, SyncOptions, SyncOperation, SyncPlan, TargetMode
from note_sync_hub.profiles import ProfileStore
from tests.test_engine import make_note


class GuiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = AppConfig(obsidian_vault_path=self.directory.name, joplin_token="test", siyuan_token="test")
        self.store = ProfileStore(Path(self.directory.name) / "profiles.json")
        for mock in (patch("note_sync_hub.gui.load_config", return_value=self.config),
                     patch("note_sync_hub.profile_panel.ProfileStore", return_value=self.store),
                     patch("note_sync_hub.profile_panel.save_config")):
            mock.start()
            self.addCleanup(mock.stop)
        try:
            self.app = SyncApp()
            self.app.withdraw()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(self.app.destroy)

    def test_saved_profile_restores_direction_folders_and_delete_policy(self):
        options = SyncOptions(mode=SyncMode.ONE_WAY, endpoints=(E.OBSIDIAN, E.SIYUAN), source=E.OBSIDIAN,
                              scope_all=False, selected_folders={E.OBSIDIAN: ("A/B",), E.SIYUAN: ()},
                              target_mode=TargetMode.SELECTED, target_folders={E.SIYUAN: "Archive"}, propagate_deletions=True)
        self.app._apply_options(options)
        self.app.profile_panel.name_var.set("归档")
        self.app.profile_panel.save()
        self.app.propagate_deletions_var.set(False)
        self.app.profile_panel.restore_last()
        self.assertEqual(self.app._collect_options(), options)
        self.app._folders_done({E.OBSIDIAN: ["", "A", "A/B"], E.SIYUAN: ["Archive"]})
        self.assertEqual(self.app._collect_options(), options)

    def test_filter_and_checkbox_preserve_hidden_row_selection(self):
        source = make_note(E.JOPLIN, native_id="j")
        operations = [SyncOperation(global_id=str(i), action=action, title=f"note-{i}", versions={E.JOPLIN: source},
                                   source=E.JOPLIN, targets=(E.OBSIDIAN,)) for i, action in enumerate((A.CREATE, A.UPDATE))]
        plan = SyncPlan(SyncOptions(mode=SyncMode.ONE_WAY, endpoints=(E.JOPLIN, E.OBSIDIAN), source=E.JOPLIN), operations, "now")
        self.app.plan = plan
        self.app._render_plan()
        panel = self.app.preview_panel
        panel.filter_var.set(A.CREATE.label)
        panel.select_visible(False)
        self.assertFalse(operations[0].selected)
        self.assertTrue(operations[1].selected)
        self.assertEqual(plan.executable_operations(), [operations[1]])
        self.app._set_busy(True)
        panel.select_visible(True)
        self.assertFalse(operations[0].selected)

    def test_close_waits_without_blocking_or_destroying_active_worker(self):
        worker = SimpleNamespace(is_alive=lambda: True)
        self.app.worker = worker
        with patch("note_sync_hub.gui.messagebox.askyesno", return_value=True), patch.object(self.app, "destroy") as destroy:
            self.app._on_close()
            self.assertTrue(self.app.cancel_event.is_set())
            self.assertTrue(self.app._closing)
            destroy.assert_not_called()
            self.app._finish_close()
            destroy.assert_not_called()
            worker.is_alive = lambda: False
            self.app._finish_close()
            destroy.assert_called_once()
