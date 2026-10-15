from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from note_sync_hub.adapters.upnote import UpNoteAdapter
from note_sync_hub.config import AppConfig, discover_upnote_data_dirs
from note_sync_hub.models import Endpoint as E, Note


class UpNoteDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "Roaming/UpNote"
        self.data.mkdir(parents=True)
        self.db = self.data / "upnote.sqlite3"
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript(
                (Path(__file__).parent / "fixtures/upnote-9.22-schema.sql").read_text(
                    encoding="utf-8"
                )
            )
            for identifier, title, parent, deleted, space in [
                ("a", "工作", None, 0, None),
                ("b", "生活", None, 0, None),
                ("c", "资料", "a", 0, None),
                ("d", "已删除", None, 1, None),
                ("e", "其他空间", None, 0, "other"),
            ]:
                c.execute(
                    "INSERT INTO notebooks(id,title,parent,deleted,space) VALUES(?,?,?,?,?)",
                    (identifier, title, parent, deleted, space),
                )
            for identifier, title in [("n1", "有目录"), ("n2", "未分类")]:
                c.execute(
                    "INSERT INTO notes(id,title,html,revision,fileIds,tagLinks,trashed,deleted) VALUES(?,?,?,1,'[]','[]',0,0)",
                    (identifier, title, "<p>正文</p>"),
                )
            c.execute(
                "INSERT INTO lists(id,content,deleted,revision) VALUES('notebooks_c',?,0,1)",
                (json.dumps(["n1"]),),
            )
            c.commit()
        self.config = AppConfig(upnote_data_dir=str(self.data))

    def test_detects_standard_location_and_deduplicates_home(self):
        with (
            patch.dict(
                os.environ,
                {
                    "APPDATA": str(self.data.parent),
                    "LOCALAPPDATA": str(self.root / "Local"),
                },
            ),
            patch("note_sync_hub.config.Path.home", return_value=self.root / "home"),
        ):
            self.assertEqual(discover_upnote_data_dirs(), [str(self.data.resolve())])
            self.db.unlink()
            self.assertEqual(discover_upnote_data_dirs(), [])

    def test_discovers_all_notebooks_and_unfiled_notes_without_writes(self):
        before = self.db.read_bytes()
        up = UpNoteAdapter(self.config)
        self.assertEqual(up.list_top_level_notebooks(), ["工作", "生活"])
        self.assertEqual(set(up.list_folders()), {"", "工作", "工作/资料", "生活"})
        self.assertEqual(
            {n.native_id: n.folder for n in up.list_notes()},
            {"n1": "工作/资料", "n2": ""},
        )
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.data / "Note Sync Hub Backups").exists())

    def test_all_notebooks_create_move_unfile_and_trash(self):
        up = UpNoteAdapter(self.config)
        source = Note(E.OBSIDIAN, "s", "", "新笔记", "", "正文")
        with patch(
            "note_sync_hub.adapters.upnote.is_upnote_running", return_value=False
        ):
            identifier = up.upsert_note(source, None, "工作/新增", "")
            self.assertEqual(up.read_note(identifier).folder, "工作/新增")
            current = up.read_note(identifier)
            up.upsert_note(current, current, "", "")
            self.assertEqual(up.read_note(identifier).folder, "")
            up.move_to_trash(up.read_note(identifier))
            self.assertIsNone(up.read_note(identifier))
        self.assertEqual({n.native_id for n in up.list_notes()}, {"n1", "n2"})

    def test_legacy_single_notebook_config_keeps_relative_scope(self):
        up = UpNoteAdapter(replace(self.config, upnote_notebook="工作"))
        self.assertEqual(set(up.list_folders()), {"", "资料"})
        self.assertEqual(
            {n.native_id: n.folder for n in up.list_notes()}, {"n1": "资料"}
        )
        self.assertNotEqual(
            self.config.state_path(),
            replace(self.config, upnote_notebook="工作").state_path(),
        )

    def test_orphaned_tree_cannot_look_like_deleted_notes(self):
        with closing(sqlite3.connect(self.db)) as c:
            c.execute(
                "UPDATE notebooks SET parent='missing' WHERE COALESCE(parent,'')=''"
            )
            c.commit()
        with self.assertRaisesRegex(Exception, "父级|循环"):
            UpNoteAdapter(self.config).list_notes()


if __name__ == "__main__":
    unittest.main()
