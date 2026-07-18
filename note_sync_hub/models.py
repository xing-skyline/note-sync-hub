from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple


ROOT_FOLDER_LABEL = "（根目录）"


def normalize_folder(value: str) -> str:
    normalized = (value or "").strip().replace("\\", "/").strip("/")
    if normalized in {".", ROOT_FOLDER_LABEL}:
        return ""
    return "/".join(part.strip() for part in normalized.split("/") if part.strip())


def folder_leaf(value: str) -> str:
    normalized = normalize_folder(value)
    return normalized.rsplit("/", 1)[-1] if normalized else ""


class Endpoint(str, Enum):
    JOPLIN = "joplin"
    OBSIDIAN = "obsidian"
    SIYUAN = "siyuan"

    @property
    def label(self) -> str:
        return {
            self.JOPLIN: "Joplin",
            self.OBSIDIAN: "Obsidian",
            self.SIYUAN: "思源笔记",
        }[self]


class SyncMode(str, Enum):
    ONE_WAY = "one_way"
    BIDIRECTIONAL = "bidirectional"

    @property
    def label(self) -> str:
        return {
            self.ONE_WAY: "单向同步",
            self.BIDIRECTIONAL: "所选端双向同步",
        }[self]


class TargetMode(str, Enum):
    PRESERVE = "preserve"
    SELECTED = "selected"
    ROOT = "root"

    @property
    def label(self) -> str:
        return {
            self.PRESERVE: "保持原目录位置",
            self.SELECTED: "放入目标端所选文件夹",
            self.ROOT: "放入目标端根目录",
        }[self]


class OperationAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    MOVE = "move"
    DELETE = "delete"
    LINK = "link"
    CONFLICT = "conflict"
    SKIP = "skip"

    @property
    def label(self) -> str:
        return {
            self.CREATE: "新建",
            self.UPDATE: "更新",
            self.MOVE: "移动/更新",
            self.DELETE: "删除",
            self.LINK: "建立关联",
            self.CONFLICT: "冲突",
            self.SKIP: "跳过",
        }[self]


@dataclass
class Asset:
    digest: str
    filename: str
    size: int
    source_ref: str = ""
    media_type: str = "application/octet-stream"
    _loader: Optional[Callable[[], bytes]] = field(default=None, repr=False, compare=False)
    _data: Optional[bytes] = field(default=None, repr=False, compare=False)

    def load(self) -> bytes:
        if self._data is None:
            if self._loader is None:
                raise ValueError(f"附件没有可用的读取器：{self.filename}")
            self._data = self._loader()
        digest = hashlib.sha256(self._data).hexdigest()
        if digest != self.digest:
            raise ValueError(f"附件在预览后发生变化：{self.filename}")
        return self._data


@dataclass
class Note:
    endpoint: Endpoint
    native_id: str
    global_id: str
    title: str
    folder: str
    body: str
    tags: Tuple[str, ...] = ()
    updated: int = 0
    revision: str = ""
    locator: str = ""
    assets: Dict[str, Asset] = field(default_factory=dict, repr=False)
    native: Dict[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.folder = normalize_folder(self.folder)
        self.tags = tuple(dict.fromkeys(tag.strip().lstrip("#") for tag in self.tags if tag.strip().lstrip("#")))

    @property
    def path_key(self) -> str:
        path = "/".join(part for part in (self.folder, self.title.strip()) if part)
        return path.casefold()

    @property
    def content_signature(self) -> str:
        normalized_body = self.body.replace("\r\n", "\n").replace("\r", "\n")
        normalized_body = "\n".join(line.rstrip() for line in normalized_body.split("\n")).strip()
        payload = {
            "body": normalized_body,
            "tags": sorted(tag.casefold() for tag in self.tags),
            "assets": sorted(self.assets),
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @property
    def snapshot_key(self) -> str:
        payload = "|".join(
            [
                self.endpoint.value,
                self.native_id,
                self.revision,
                self.content_signature,
                self.title,
                self.folder,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def relocated(self, folder: str, title: Optional[str] = None) -> "Note":
        return replace(self, folder=normalize_folder(folder), title=title or self.title)


@dataclass
class SyncOptions:
    mode: SyncMode
    endpoints: Tuple[Endpoint, ...]
    source: Optional[Endpoint] = None
    primary: Optional[Endpoint] = None
    scope_all: bool = True
    selected_folders: Dict[Endpoint, Tuple[str, ...]] = field(default_factory=dict)
    include_subfolders: bool = True
    target_mode: TargetMode = TargetMode.PRESERVE
    target_folders: Dict[Endpoint, str] = field(default_factory=dict)
    propagate_deletions: bool = False

    @property
    def targets(self) -> Tuple[Endpoint, ...]:
        if self.mode != SyncMode.ONE_WAY or self.source is None:
            return ()
        return tuple(endpoint for endpoint in self.endpoints if endpoint != self.source)

    def validate(self) -> None:
        unique = tuple(dict.fromkeys(self.endpoints))
        if len(unique) < 2:
            raise ValueError("请至少选择两个笔记端。")
        if len(unique) != len(self.endpoints):
            raise ValueError("笔记端选择存在重复。")
        if self.mode == SyncMode.ONE_WAY:
            if self.source not in unique:
                raise ValueError("单向同步必须选择一个来源端。")
            if not self.targets:
                raise ValueError("单向同步必须至少选择一个目标端。")
            if self.primary is not None:
                raise ValueError("单向同步不应指定双向主端。")
        elif self.source is not None:
            raise ValueError("双向同步不应指定固定来源端。")
        elif self.primary is None:
            # 保留对早期配置/调用方的兼容；桌面界面始终要求用户明确显示主端。
            self.primary = unique[0]
        elif self.primary not in unique:
            raise ValueError("双向同步必须从参与端中选择一个主端。")

        if not self.scope_all:
            required = (self.source,) if self.mode == SyncMode.ONE_WAY else unique
            for endpoint in required:
                if endpoint is not None and not self.selected_folders.get(endpoint):
                    raise ValueError(f"请为 {endpoint.label} 选择至少一个文件夹。")

        if self.mode == SyncMode.ONE_WAY and self.target_mode != TargetMode.PRESERVE:
            if self.scope_all:
                raise ValueError("目标目录映射仅适用于“仅所选文件夹”。")
            sources = [normalize_folder(value) for value in self.selected_folders.get(self.source, ())]
            self._validate_mapping_sources(sources)
            if self.target_mode == TargetMode.SELECTED:
                for endpoint in self.targets:
                    if not normalize_folder(self.target_folders.get(endpoint, "")):
                        raise ValueError(f"请为 {endpoint.label} 选择一个目标文件夹。")

    def _validate_mapping_sources(self, sources: Sequence[str]) -> None:
        normalized = list(dict.fromkeys(sources))
        if any(not value for value in normalized):
            raise ValueError("目录映射不支持把根目录作为来源。")
        if self.include_subfolders:
            for index, left in enumerate(normalized):
                for right in normalized[index + 1 :]:
                    if left.startswith(right + "/") or right.startswith(left + "/"):
                        raise ValueError("包含子文件夹时，不能同时选择父文件夹和子文件夹。")
        leaves = [folder_leaf(value).casefold() for value in normalized]
        if len(leaves) != len(set(leaves)):
            raise ValueError("多个来源文件夹的末级名称相同，目标路径会发生冲突。")

    def includes(self, endpoint: Endpoint, folder: str) -> bool:
        if self.scope_all:
            return True
        normalized = normalize_folder(folder)
        for selected in self.selected_folders.get(endpoint, ()):
            candidate = normalize_folder(selected)
            if normalized == candidate:
                return True
            if self.include_subfolders and candidate and normalized.startswith(candidate + "/"):
                return True
            if self.include_subfolders and not candidate:
                return True
        return False

    def includes_note(self, note: "Note") -> bool:
        if self.includes(note.endpoint, note.folder):
            return True
        if self.scope_all:
            return True
        # 在思源中，文档本身也可以充当下一层文档的“文件夹”。选择某个
        # 文档时，应同时包含该文档和它的子文档。
        if note.endpoint != Endpoint.SIYUAN:
            return False
        full_path = normalize_folder("/".join(part for part in (note.folder, note.title) if part))
        for selected in self.selected_folders.get(note.endpoint, ()):
            candidate = normalize_folder(selected)
            if full_path == candidate:
                return True
            if self.include_subfolders and candidate and full_path.startswith(candidate + "/"):
                return True
        return False

    def mapped_target_folder(self, source_folder: str, target: Endpoint) -> str:
        source_folder = normalize_folder(source_folder)
        if self.mode != SyncMode.ONE_WAY or self.target_mode == TargetMode.PRESERVE:
            return source_folder
        source_roots = [normalize_folder(value) for value in self.selected_folders.get(self.source, ())]
        applicable = [
            root
            for root in source_roots
            if source_folder == root or (self.include_subfolders and source_folder.startswith(root + "/"))
        ]
        if not applicable:
            return source_folder
        root = max(applicable, key=lambda value: len(value.split("/")))
        suffix = source_folder[len(root) :].strip("/")
        relative = "/".join(part for part in (folder_leaf(root), suffix) if part)
        if self.target_mode == TargetMode.ROOT:
            return normalize_folder(relative)
        base = normalize_folder(self.target_folders.get(target, ""))
        return normalize_folder("/".join(part for part in (base, relative) if part))


@dataclass
class SyncOperation:
    global_id: str
    action: OperationAction
    title: str
    versions: Dict[Endpoint, Note]
    source: Optional[Endpoint] = None
    targets: Tuple[Endpoint, ...] = ()
    target_folders: Dict[Endpoint, str] = field(default_factory=dict)
    reason: str = ""
    state_record: Optional[Dict[str, object]] = field(default=None, repr=False)
    resolved_note: Optional[Note] = field(default=None, repr=False)
    keep_separate: bool = False

    @property
    def executable(self) -> bool:
        if self.action in {OperationAction.CONFLICT, OperationAction.SKIP}:
            return self.resolved_note is not None and bool(self.targets)
        if self.action == OperationAction.DELETE:
            return bool(self.targets)
        if self.action == OperationAction.LINK:
            return bool(self.versions)
        return self.source is not None and bool(self.targets)

    @property
    def source_note(self) -> Optional[Note]:
        if self.resolved_note is not None:
            return self.resolved_note
        return self.versions.get(self.source) if self.source is not None else None

    @property
    def direction_label(self) -> str:
        if self.action == OperationAction.DELETE:
            return "删除 → " + "、".join(endpoint.label for endpoint in self.targets)
        if self.source is None:
            return "待处理"
        return f"{self.source.label} → " + "、".join(endpoint.label for endpoint in self.targets)


@dataclass
class SyncPlan:
    options: SyncOptions
    operations: List[SyncOperation]
    scanned_at: str
    scan_fingerprints: Dict[Endpoint, Dict[str, str]] = field(default_factory=dict, repr=False)

    def counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for operation in self.operations:
            counts[operation.action.value] = counts.get(operation.action.value, 0) + 1
        return counts

    def executable_operations(self) -> List[SyncOperation]:
        return [operation for operation in self.operations if operation.executable]


@dataclass
class ExecutionResult:
    completed: int
    skipped: int
    errors: List[str] = field(default_factory=list)
