from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import List, Optional, Tuple


class DiffChoice(str, Enum):
    UNDECIDED = "undecided"
    USE_LEFT = "use_left"
    USE_RIGHT = "use_right"
    KEEP_BOTH = "keep_both"
    KEEP_SEPARATE = "keep_separate"

    def label(self, left_label: str, right_label: str) -> str:
        return {
            self.UNDECIDED: "尚未选择",
            self.USE_LEFT: f"采用 {left_label}",
            self.USE_RIGHT: f"采用 {right_label}",
            self.KEEP_BOTH: "两份都保留",
            self.KEEP_SEPARATE: "保持两端各自内容",
        }[self]


@dataclass
class DiffSegment:
    index: int
    tag: str
    left_lines: List[str]
    right_lines: List[str]
    choice: DiffChoice = DiffChoice.UNDECIDED

    @property
    def is_equal(self) -> bool:
        return self.tag == "equal"

    @property
    def kind_label(self) -> str:
        return {
            "equal": "相同",
            "replace": "内容不同",
            "delete": "仅左侧有",
            "insert": "仅右侧有",
        }.get(self.tag, self.tag)

    @staticmethod
    def _preview(lines: List[str], limit: int = 1000) -> str:
        value = "".join(lines).strip()
        return value if len(value) <= limit else value[:limit] + "…"

    @property
    def left_preview(self) -> str:
        return self._preview(self.left_lines)

    @property
    def right_preview(self) -> str:
        return self._preview(self.right_lines)


@dataclass
class NoteDiff:
    left_label: str
    right_label: str
    segments: List[DiffSegment] = field(default_factory=list)

    @property
    def differences(self) -> List[DiffSegment]:
        return [segment for segment in self.segments if not segment.is_equal]

    @property
    def unresolved_count(self) -> int:
        return sum(segment.choice == DiffChoice.UNDECIDED for segment in self.differences)

    def choose_all(self, choice: DiffChoice) -> None:
        for segment in self.differences:
            segment.choice = choice

    def render(self) -> Tuple[str, str]:
        if self.unresolved_count:
            raise ValueError(f"还有 {self.unresolved_count} 个差异块尚未选择处理方式。")
        left_output: List[str] = []
        right_output: List[str] = []
        for segment in self.segments:
            if segment.is_equal:
                left_output.extend(segment.left_lines)
                right_output.extend(segment.right_lines)
            elif segment.choice == DiffChoice.USE_LEFT:
                left_output.extend(segment.left_lines)
                right_output.extend(segment.left_lines)
            elif segment.choice == DiffChoice.USE_RIGHT:
                left_output.extend(segment.right_lines)
                right_output.extend(segment.right_lines)
            elif segment.choice == DiffChoice.KEEP_SEPARATE:
                left_output.extend(segment.left_lines)
                right_output.extend(segment.right_lines)
            elif segment.choice == DiffChoice.KEEP_BOTH:
                combined = _combine_both(
                    segment.left_lines,
                    segment.right_lines,
                    self.right_label,
                )
                left_output.extend(combined)
                right_output.extend(combined)
            else:
                raise ValueError("差异块存在无效选择。")
        return "".join(left_output), "".join(right_output)


def _ensure_line_break(lines: List[str]) -> List[str]:
    if not lines:
        return []
    result = list(lines)
    if not result[-1].endswith(("\n", "\r")):
        result[-1] += "\n"
    return result


def _combine_both(left: List[str], right: List[str], right_label: str) -> List[str]:
    if not left:
        return list(right)
    if not right:
        return list(left)
    combined = _ensure_line_break(left)
    combined.extend(
        [
            "\n",
            f"<!-- Note Sync Hub：以下为 {right_label} 保留的差异内容 -->\n",
            "\n",
        ]
    )
    combined.extend(right)
    return combined


def build_note_diff(
    left_body: str,
    right_body: str,
    left_label: str,
    right_label: str,
    default_side: Optional[str] = None,
) -> NoteDiff:
    left_lines = (left_body or "").splitlines(keepends=True)
    right_lines = (right_body or "").splitlines(keepends=True)
    matcher = SequenceMatcher(None, left_lines, right_lines, autojunk=False)
    default_choice = DiffChoice.UNDECIDED
    if default_side == "left":
        default_choice = DiffChoice.USE_LEFT
    elif default_side == "right":
        default_choice = DiffChoice.USE_RIGHT
    segments: List[DiffSegment] = []
    for index, (tag, i1, i2, j1, j2) in enumerate(matcher.get_opcodes(), 1):
        segments.append(
            DiffSegment(
                index=index,
                tag=tag,
                left_lines=left_lines[i1:i2],
                right_lines=right_lines[j1:j2],
                choice=DiffChoice.KEEP_SEPARATE if tag == "equal" else default_choice,
            )
        )
    return NoteDiff(left_label=left_label, right_label=right_label, segments=segments)
