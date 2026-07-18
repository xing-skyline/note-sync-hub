from __future__ import annotations

import unittest

from note_sync_hub.diffmerge import DiffChoice, build_note_diff


class DiffMergeTests(unittest.TestCase):
    def test_every_difference_requires_an_explicit_choice(self):
        note_diff = build_note_diff("共同\n左侧\n", "共同\n右侧\n", "Joplin", "Obsidian")
        self.assertEqual(note_diff.unresolved_count, 1)
        with self.assertRaisesRegex(ValueError, "尚未选择"):
            note_diff.render()

    def test_choose_left_converges_both_outputs(self):
        note_diff = build_note_diff("共同\n左侧\n", "共同\n右侧\n", "Joplin", "Obsidian")
        note_diff.choose_all(DiffChoice.USE_LEFT)
        left, right = note_diff.render()
        self.assertEqual(left, "共同\n左侧\n")
        self.assertEqual(right, left)

    def test_keep_both_marks_the_appended_variant(self):
        note_diff = build_note_diff("左侧\n", "右侧\n", "Joplin", "思源笔记")
        note_diff.choose_all(DiffChoice.KEEP_BOTH)
        left, right = note_diff.render()
        self.assertEqual(left, right)
        self.assertIn("左侧", left)
        self.assertIn("右侧", left)
        self.assertIn("Note Sync Hub", left)
        self.assertIn("思源笔记", left)


if __name__ == "__main__":
    unittest.main()
