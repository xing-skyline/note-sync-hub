from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from note_sync_hub.adapters.base import AdapterError
from note_sync_hub.adapters.obsidian import ObsidianAdapter
from note_sync_hub.adapters.upnote import UpNoteAdapter
from note_sync_hub.config import AppConfig
from note_sync_hub.engine import SyncEngine
from note_sync_hub.models import (
    Endpoint as E,
    Note,
    OperationAction as A,
    SyncMode,
    SyncOptions,
)
from note_sync_hub.state import StateStore


class UpNoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "upnote"
        self.data.mkdir()
        self.db = self.data / "upnote.sqlite3"
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript(
                (Path(__file__).parent / "fixtures/upnote-9.22-schema.sql").read_text(
                    encoding="utf-8"
                )
            )
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.config = AppConfig(
            obsidian_vault_path=str(self.vault),
            upnote_data_dir=str(self.data),
            upnote_notebook="同步测试",
        )
        self.up = UpNoteAdapter(self.config)
        self.ob = ObsidianAdapter(self.config)
        self.patch = patch(
            "note_sync_hub.adapters.upnote.is_upnote_running", return_value=False
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def source(self, **changes):
        return replace(
            Note(E.OBSIDIAN, "source", "", "测试笔记", "", "正文 **内容**\n"), **changes
        )

    def execute(self, actions):
        engine = SyncEngine(
            self.config,
            adapters={E.UPNOTE: self.up, E.OBSIDIAN: self.ob},
            state_store=StateStore(self.root / "state.json"),
        )
        options = SyncOptions(
            SyncMode.BIDIRECTIONAL, (E.UPNOTE, E.OBSIDIAN), primary=E.UPNOTE
        )
        plan = engine.preview(options)
        self.assertEqual([op.action for op in plan.operations], actions)
        result = engine.execute(plan)
        self.assertEqual(result.errors, [])

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute(sql, params).fetchall()

    def test_engine_roundtrip_and_attachment_dedup(self):
        assets = self.vault / "attachments"
        assets.mkdir()
        (assets / "test.pdf").write_bytes(b"%PDF-1.4\nasset-one")
        path = self.vault / "测试笔记.md"
        path.write_text(
            "来自 Obsidian\n\n[PDF](attachments/test.pdf)\n", encoding="utf-8"
        )
        self.execute([A.CREATE])
        first = self.up.list_notes()[0]
        self.execute([])
        self.assertEqual(self.query("SELECT count(*) FROM files")[0][0], 1)
        path.write_text(
            path.read_text(encoding="utf-8") + "\n修改正文\n", encoding="utf-8"
        )
        self.execute([A.UPDATE])
        self.execute([])
        self.assertEqual(self.up.list_notes()[0].native_id, first.native_id)
        self.assertEqual(self.query("SELECT count(*) FROM files")[0][0], 1)
        (assets / "test.pdf").write_bytes(b"%PDF-1.4\nasset-two")
        self.execute([A.UPDATE])
        self.execute([])
        self.assertEqual(self.query("SELECT count(*) FROM files")[0][0], 2)
        current = self.up.read_note(first.native_id)
        self.up.upsert_note(
            replace(current, body=current.body + "\n\n来自 UpNote"), current, "", ""
        )
        self.execute([A.UPDATE])
        self.execute([])
        self.assertIn("来自 UpNote", path.read_text(encoding="utf-8"))
        self.assertEqual(
            self.up.read_note(first.native_id).assets.keys(),
            self.ob.list_notes()[0].assets.keys(),
        )

    def test_running_app_refuses_write_without_backup_or_change(self):
        before = self.db.read_bytes()
        with patch(
            "note_sync_hub.adapters.upnote.is_upnote_running", return_value=True
        ):
            with self.assertRaisesRegex(AdapterError, "退出"):
                self.up.upsert_note(self.source(), None, "", "")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse((self.data / "Note Sync Hub Backups").exists())

    def test_backup_schema_and_scope(self):
        native_id = self.up.upsert_note(self.source(), None, "部门/资料", "")
        self.assertEqual(self.up.read_note(native_id).folder, "部门/资料")
        self.assertEqual(set(self.up.list_folders()), {"", "部门", "部门/资料"})
        other = UpNoteAdapter(replace(self.config, upnote_notebook="其他笔记本"))
        other.upsert_note(self.source(title="不在范围内"), None, "", "")
        self.assertEqual(len(self.up.list_notes()), 1)
        self.assertIsNone(self.up.read_note(other.list_notes()[0].native_id))
        backups = list((self.data / "Note Sync Hub Backups").glob("*/upnote.sqlite3"))
        self.assertGreaterEqual(len(backups), 2)
        with closing(sqlite3.connect(backups[0])) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_tags_survive_body_edit_and_do_not_add_body_metadata(self):
        source = self.source(tags=("工作", "todo"))
        native_id = self.up.upsert_note(source, None, "", "")
        actual = self.up.read_note(native_id)
        self.assertEqual(set(actual.tags), set(source.tags))
        self.assertTrue(self.up.matches_written(actual, source, ""))
        self.assertNotIn("#todo", actual.body)
        self.up.upsert_note(replace(actual, body="更新正文"), actual, "", "")
        self.assertEqual(set(self.up.read_note(native_id).tags), set(source.tags))

    def test_rename_move_retains_id_and_other_notebook_memberships(self):
        native_id = self.up.upsert_note(self.source(), None, "旧目录", "")
        with closing(sqlite3.connect(self.db)) as c:
            c.execute(
                "INSERT INTO notebooks(id,title,deleted) VALUES('outside','其他',0)"
            )
            c.execute(
                "INSERT INTO lists(id,content,deleted,revision) VALUES('notebooks_outside',?,0,1)",
                (json.dumps([native_id]),),
            )
            c.commit()
        actual = self.up.read_note(native_id)
        self.up.upsert_note(replace(actual, title="改名"), actual, "新目录", "")
        moved = self.up.read_note(native_id)
        self.assertEqual((moved.title, moved.folder), ("改名", "新目录"))
        self.assertEqual(
            json.loads(
                self.query("SELECT content FROM lists WHERE id='notebooks_outside'")[0][
                    0
                ]
            ),
            [native_id],
        )

    def test_stale_target_rejected(self):
        native_id = self.up.upsert_note(self.source(), None, "", "")
        old = self.up.read_note(native_id)
        self.up.upsert_note(replace(old, body="新版本"), old, "", "")
        with self.assertRaisesRegex(AdapterError, "变化"):
            self.up.upsert_note(replace(old, body="过期版本"), old, "", "")
        self.assertIn("新版本", self.up.read_note(native_id).body)

    def test_rich_target_not_silently_overwritten_and_frontmatter_rejected(self):
        native_id = self.up.upsert_note(self.source(), None, "", "")
        with closing(sqlite3.connect(self.db)) as c:
            c.execute(
                "UPDATE notes SET html=html||'<div style=\"color:red\">红色</div>',revision=revision+1 WHERE id=?",
                (native_id,),
            )
            c.commit()
        actual = self.up.read_note(native_id)
        with self.assertRaisesRegex(AdapterError, "格式"):
            self.up.upsert_note(self.source(), actual, "", "")
        with self.assertRaisesRegex(AdapterError, "YAML"):
            self.up.upsert_note(
                self.source(body="---\naliases: [a]\n---\n正文"), None, "", ""
            )

    def test_trash_is_reversible_and_outside_scope_blocked(self):
        native_id = self.up.upsert_note(self.source(), None, "", "")
        self.up.move_to_trash(self.up.read_note(native_id))
        self.assertIsNone(self.up.read_note(native_id))
        self.assertEqual(
            self.query("SELECT trashed,deleted FROM notes WHERE id=?", (native_id,))[0],
            (1, 0),
        )

    def test_inline_tags_preserve_visible_prose(self):
        source = self.source(body="这段正文包含 #工作 标签。", tags=("工作",))
        identifier = self.up.upsert_note(source, None, "", "")
        self.assertTrue(
            self.up.matches_written(self.up.read_note(identifier), source, "")
        )

    def test_empty_image_alt_roundtrip_and_localhost_link(self):
        (self.vault / "a.png").write_bytes(b"image-data")
        (self.vault / "图片.md").write_text(
            "![](a.png)\n\n[本地服务](http://localhost:8080/status)\n", encoding="utf-8"
        )
        self.execute([A.CREATE])
        self.execute([])

    def test_tag_only_paragraph_keeps_source_text(self):
        source = self.source(body="#工作 #todo\n\n正文", tags=("工作", "todo"))
        identifier = self.up.upsert_note(source, None, "", "")
        actual = self.up.read_note(identifier)
        self.assertTrue(self.up.matches_written(actual, source, ""))
        self.assertIn("#工作 #todo", actual.body)

    def test_html_comments_are_not_silently_lost(self):
        with self.assertRaisesRegex(AdapterError, "格式"):
            self.up.upsert_note(
                self.source(body="正文\n\n<!-- 保留的批注 -->"), None, "", ""
            )

    def test_unknown_format_is_rejected_before_writing(self):
        with self.assertRaisesRegex(AdapterError, "格式"):
            self.up.upsert_note(
                self.source(body="<p>有<mark>高亮</mark></p>"), None, "", ""
            )
        self.assertEqual(self.query("SELECT count(*) FROM notes")[0][0], 0)

    def test_connection_identity_preserves_legacy_and_separates_upnote(self):
        config = AppConfig(obsidian_vault_path=str(self.vault))
        identity = "|".join(
            [
                config.joplin_api_base.casefold(),
                str(self.vault.resolve()).casefold(),
                config.siyuan_api_base.casefold(),
            ]
        )
        self.assertEqual(
            config.state_path().stem, hashlib.sha256(identity.encode()).hexdigest()[:16]
        )
        self.assertNotEqual(
            self.config.state_path(),
            replace(self.config, upnote_notebook="另一范围").state_path(),
        )

    def test_four_endpoint_engine_and_joplin_siyuan_routes(self):
        from tests.test_engine import FakeAdapter

        config = replace(self.config, joplin_token="test", siyuan_token="test")
        ja = FakeAdapter(E.JOPLIN, [self.source(endpoint=E.JOPLIN)])
        sa = FakeAdapter(E.SIYUAN)
        engine = SyncEngine(
            config,
            adapters={
                E.JOPLIN: ja,
                E.SIYUAN: sa,
                E.OBSIDIAN: self.ob,
                E.UPNOTE: self.up,
            },
            state_store=StateStore(self.root / "four.json"),
        )
        options = SyncOptions(SyncMode.BIDIRECTIONAL, tuple(E), primary=E.JOPLIN)
        result = engine.execute(engine.preview(options))
        self.assertEqual(result.errors, [])
        self.assertFalse(engine.preview(options).operations)
        sa.notes[0] = replace(sa.notes[0], body="思源修改", revision="new")
        result = engine.execute(engine.preview(options))
        self.assertEqual(result.errors, [])
        self.assertEqual(self.up.list_notes()[0].body, "思源修改")
        self.assertFalse(engine.preview(options).operations)

    def test_transaction_failure_removes_only_new_blobs(self):
        (self.vault / "a.png").write_bytes(b"new-image")
        (self.vault / "图片.md").write_text("![image](a.png)", encoding="utf-8")
        source = self.ob.list_notes()[0]
        with patch.object(
            self.up, "_tags", side_effect=AdapterError("injected failure")
        ):
            with self.assertRaisesRegex(AdapterError, "injected"):
                self.up.upsert_note(source, None, "", "")
        self.assertEqual(self.query("SELECT count(*) FROM files")[0][0], 0)
        self.assertEqual(self.query("SELECT count(*) FROM notes")[0][0], 0)
        self.assertEqual(list((self.data / "images").iterdir()), [])

    def test_schema_and_missing_assets_stop_before_sync(self):
        with closing(sqlite3.connect(self.db)) as c:
            c.execute("ALTER TABLE notes RENAME COLUMN revision TO incompatible")
            c.commit()
        with self.assertRaisesRegex(AdapterError, "结构不兼容"):
            self.up.list_notes()

    def test_production_engine_registers_upnote(self):
        engine = SyncEngine(self.config)
        self.assertIsInstance(engine.adapters[E.UPNOTE], UpNoteAdapter)


if __name__ == "__main__":
    unittest.main()
