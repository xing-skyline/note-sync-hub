import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from note_sync_hub.adapters.obsidian import send_to_recycle_bin, AdapterError


@unittest.skipIf(os.name == "nt", "POSIX system-trash regression")
class SystemTrashTests(unittest.TestCase):
    def test_chinese_path_is_sent_to_system_trash(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "中文 笔记.md"
            p.write_text("test", encoding="utf-8")
            with patch("send2trash.send2trash") as trash:
                send_to_recycle_bin(p)
            trash.assert_called_once_with(str(p.resolve()))

    def test_failure_keeps_original_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "保留.md"
            p.write_text("important", encoding="utf-8")
            with patch("send2trash.send2trash", side_effect=PermissionError("denied")):
                with self.assertRaises(AdapterError):
                    send_to_recycle_bin(p)
            self.assertEqual(p.read_text(), "important")

    def test_missing_path_does_not_call_trash(self):
        with tempfile.TemporaryDirectory() as d:
            with patch("send2trash.send2trash") as trash:
                with self.assertRaises(AdapterError):
                    send_to_recycle_bin(Path(d)/"missing.md")
            trash.assert_not_called()
