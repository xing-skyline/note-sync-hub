from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from note_sync_hub.config import AppConfig
from note_sync_hub.engine import SyncEngine, SyncEngineError
from note_sync_hub.models import Endpoint as E, SyncMode, SyncOptions
from note_sync_hub.state import StateStore
from tests.test_engine import FakeAdapter, make_note


class StateSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="note-state-中文 ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "state.json"
        self.store = StateStore(self.path)
        self.groups = {"g": {"endpoints": {"joplin": {"native_id": "j"}}}}

    def test_missing_state_with_either_backup_stops(self):
        for name in ("state.json.bak", "state-20261001-120000.bak.json"):
            with self.subTest(name=name):
                backup = self.root / name
                backup.write_text('{"version": 1, "groups": {}}')
                try:
                    with self.assertRaisesRegex(ValueError, "缺失"):
                        self.store.load()
                    with self.assertRaises(ValueError):
                        self.store.save({})
                    self.assertFalse(self.path.exists())
                finally:
                    backup.unlink()

    def test_first_save_and_previous_version_are_recoverable(self):
        self.store.save(self.groups)
        first = self.path.read_bytes()
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), first)
        self.store.save({})
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), first)
        self.assertEqual(self.store.load()["groups"], {})

    def test_directory_is_not_treated_as_first_use(self):
        self.path.mkdir()
        with self.assertRaises(ValueError):
            self.store.load()

    def test_corrupt_state_cannot_be_overwritten(self):
        self.path.write_bytes(b"broken")
        with self.assertRaises(ValueError):
            self.store.save({})
        self.assertEqual(self.path.read_bytes(), b"broken")

    def test_replace_failure_keeps_main_and_backup_and_cleans_temporary(self):
        self.store.save(self.groups)
        before = self.path.read_bytes()
        replace = os.replace
        for fail_backup in (True, False):
            with self.subTest(fail_backup=fail_backup):
                def fail(source, destination):
                    target = Path(destination)
                    if target == (self.path.with_suffix(".json.bak") if fail_backup else self.path):
                        raise OSError("simulated disk failure")
                    replace(source, destination)
                with patch("note_sync_hub.state.os.replace", side_effect=fail):
                    with self.assertRaises(OSError):
                        self.store.save({})
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), before)
                self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_existing_fixed_temporary_is_not_touched(self):
        stale = self.path.with_suffix(".json.tmp")
        stale.write_bytes(b"interrupted operation evidence")
        self.store.save(self.groups)
        self.assertEqual(stale.read_bytes(), b"interrupted operation evidence")

    def test_invalid_new_state_does_not_change_existing_files(self):
        self.store.save(self.groups)
        before = self.path.read_bytes()
        for invalid in ({"g": {"endpoints": []}}, {"g": {"endpoints": {"joplin": object()}}}):
            with self.subTest(invalid=invalid):
                with self.assertRaises((ValueError, TypeError)):
                    self.store.save(invalid)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(self.root.glob("*.tmp")), [])

    def child_save(self):
        script = "from pathlib import Path; from note_sync_hub.state import StateStore; import sys; StateStore(Path(sys.argv[1])).save({})"
        return subprocess.run([sys.executable, "-c", script, str(self.path)],
                              cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)

    def test_lock_excludes_other_process_and_releases_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.lock():
                StateStore(self.path).save(self.groups)  # same-thread reentrancy
                self.assertNotEqual(self.child_save().returncode, 0)
                self.assertEqual(self.store.load()["groups"], self.groups)
                raise RuntimeError("abort")
        self.assertEqual(self.child_save().returncode, 0)

    def make_engine(self):
        config = AppConfig(obsidian_vault_path=str(self.root), joplin_token="test")
        source = FakeAdapter(E.JOPLIN, [make_note(E.JOPLIN, native_id="j")])
        target = FakeAdapter(E.OBSIDIAN)
        return SyncEngine(config, adapters={E.JOPLIN: source, E.OBSIDIAN: target}, state_store=self.store)

    def test_process_exit_releases_lock_without_deleting_lock_file(self):
        script = ("from pathlib import Path; from note_sync_hub.state import StateStore; import sys, os; "
                  "guard=StateStore(Path(sys.argv[1])).lock(); guard.__enter__(); os._exit(9)")
        result = subprocess.run([sys.executable, "-c", script, str(self.path)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 9)
        self.assertTrue(self.path.with_suffix(".json.lock").exists())
        self.assertEqual(self.child_save().returncode, 0)

    def test_preview_scan_also_holds_lock(self):
        engine = self.make_engine()
        original = engine.scan
        def scan(endpoints):
            self.assertNotEqual(self.child_save().returncode, 0)
            return original(endpoints)
        engine.scan = scan
        engine.preview(SyncOptions(mode=SyncMode.ONE_WAY, endpoints=(E.JOPLIN, E.OBSIDIAN), source=E.JOPLIN))

    def test_state_change_alone_invalidates_preview_before_any_note_write(self):
        engine = self.make_engine()
        options = SyncOptions(mode=SyncMode.ONE_WAY, endpoints=(E.JOPLIN, E.OBSIDIAN), source=E.JOPLIN)
        plan = engine.preview(options)
        self.store.save(self.groups)
        with self.assertRaisesRegex(SyncEngineError, "状态"):
            engine.execute(plan)
        self.assertEqual(engine.adapters[E.JOPLIN].links, [])
        self.assertEqual(engine.adapters[E.OBSIDIAN].writes, [])

    def test_execution_holds_lock_until_checkpoint_is_saved(self):
        engine = self.make_engine()
        options = SyncOptions(mode=SyncMode.ONE_WAY, endpoints=(E.JOPLIN, E.OBSIDIAN), source=E.JOPLIN)
        plan = engine.preview(options)
        attempts = []
        def progress(*args):
            attempts.append(self.child_save().returncode)
        result = engine.execute(plan, progress=progress)
        self.assertEqual(result.completed, 1)
        self.assertTrue(attempts)
        self.assertTrue(all(code != 0 for code in attempts))
        self.assertTrue(self.store.load()["groups"])


if __name__ == "__main__":
    unittest.main()
