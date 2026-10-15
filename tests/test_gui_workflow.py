import tempfile
from contextlib import closing
import sqlite3
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
        original_init = tk.Tk.__init__

        def hidden_init(root, *args, **kwargs):
            original_init(root, *args, **kwargs)
            root.withdraw()

        for mock in (patch("note_sync_hub.gui.load_config", return_value=self.config),
                     patch.object(tk.Tk, '__init__', hidden_init),
                     patch.object(SyncApp, 'state', return_value='withdrawn'),
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

    def test_upnote_connection_and_bidirectional_selection(self):
        self.assertFalse(self.app.endpoint_vars[E.UPNOTE].get())
        self.app.upnote_data_var.set('C:/test/UpNote')
        self.app.upnote_notebook_var.set('同步笔记本')
        config = self.app._collect_config()
        self.assertEqual(config.upnote_notebook, '同步笔记本')
        self.assertEqual(config.upnote_data_dir, 'C:/test/UpNote')
        options = SyncOptions(SyncMode.BIDIRECTIONAL, (E.OBSIDIAN, E.UPNOTE), primary=E.UPNOTE,
                              selected_folders={E.OBSIDIAN: (), E.UPNOTE: ()})
        self.app._apply_options(options)
        self.assertEqual(self.app._collect_options(), options)

    def test_upnote_enable_auto_fills_location_and_notebook_folders(self):
        data = Path(self.directory.name) / 'UpNote'
        data.mkdir()
        db = data / 'upnote.sqlite3'
        with closing(sqlite3.connect(db)) as c:
            c.executescript((Path(__file__).parent/'fixtures/upnote-9.22-schema.sql').read_text(encoding='utf-8'))
            c.execute("INSERT INTO notebooks(id,title,deleted) VALUES('book','自动读取的笔记本',0)")
            c.commit()
        before = db.read_bytes()
        with patch('note_sync_hub.gui.discover_upnote_data_dirs', return_value=[str(data)]):
            self.app.endpoint_vars[E.UPNOTE].set(True)
            self.app._toggle_options()
        self.assertEqual(self.app._collect_config().upnote_data_dir, str(data))
        self.assertEqual(self.app._collect_config().upnote_notebook, '')
        self.assertIn('自动读取的笔记本', self.app.folder_values[E.UPNOTE])
        self.assertIn('仅：自动读取的笔记本', self.app.upnote_scope_combo['values'])
        self.app._discover_upnote(force=True)
        self.assertEqual(str(self.app.folder_lists[E.UPNOTE]['state']), 'disabled')
        self.assertEqual(db.read_bytes(), before)

    def test_auto_detect_preserves_manual_location_and_ambiguous_choices(self):
        self.app.upnote_data_var.set('D:/custom-data')
        with patch('note_sync_hub.gui.discover_upnote_data_dirs') as detect:
            self.app._discover_upnote(force=True)
            detect.assert_not_called()
        self.assertEqual(self.app.upnote_data_var.get(), 'D:/custom-data')
        self.app.upnote_data_var.set('')
        with patch('note_sync_hub.gui.discover_upnote_data_dirs', return_value=['D:/one','D:/two']):
            self.app._discover_upnote(force=True)
        self.assertEqual(self.app.upnote_data_var.get(), '')
        self.assertIn('多个', self.app.status_var.get())

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
