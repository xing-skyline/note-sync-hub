import tempfile
import unittest
from pathlib import Path

from note_sync_hub.models import Endpoint, SyncMode, SyncOptions, TargetMode
from note_sync_hub.profiles import ProfileStore


class ProfileTests(unittest.TestCase):
    def test_round_trip_preserves_complete_sync_options(self):
        options = SyncOptions(
            mode=SyncMode.ONE_WAY, endpoints=(Endpoint.OBSIDIAN, Endpoint.SIYUAN),
            source=Endpoint.OBSIDIAN, scope_all=False,
            selected_folders={Endpoint.OBSIDIAN: ("课程/教学",)}, include_subfolders=False,
            target_mode=TargetMode.SELECTED, target_folders={Endpoint.SIYUAN: "资料库/归档"},
            propagate_deletions=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            ProfileStore(path).save("课程归档", options, "connection-a")
            store = ProfileStore(path)
            self.assertEqual(store.get("课程归档", "connection-a"), options)
            self.assertEqual(store.last_name("connection-a"), "课程归档")
            self.assertEqual(store.names("connection-b"), [])
            with self.assertRaises(ValueError):
                store.get("课程归档", "connection-b")

    def test_malformed_profile_is_rejected_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text('{"profiles":', encoding="utf-8")
            with self.assertRaises(ValueError):
                ProfileStore(path).names("connection-a")
            self.assertEqual(path.read_text(encoding="utf-8"), '{"profiles":')
