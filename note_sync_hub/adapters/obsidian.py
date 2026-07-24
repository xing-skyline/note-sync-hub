from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from ..attachments import (
    AttachmentIssue,
    ResolvedAttachment,
    WINDOWS_ABSOLUTE_RE,
    attachment_references,
    bytes_sha256,
    canonical_asset_uri,
    normalized_local_target,
    replace_canonical_asset_uris,
    replace_reference_targets,
)
from ..config import AppConfig
from ..metadata import (
    SyncMetadata,
    apply_obsidian_metadata,
    extract_obsidian_metadata,
    extract_obsidian_tags,
    obsidian_metadata_needs_repair,
    strip_obsidian_metadata,
)
from ..models import Asset, Endpoint, Note, normalize_folder
from .base import AdapterError, NoteAdapter


LEGACY_TRASH_FOLDER = ".joplin-obsidian-sync-trash"


class _SHFileOperation(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def send_to_windows_recycle_bin(path: Path) -> None:
    if os.name != "nt":
        raise AdapterError("当前系统不支持 Windows 回收站。")
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise AdapterError(f"要删除的 Obsidian 文件不存在：{resolved}")
    operation = _SHFileOperation()
    operation.wFunc = 3
    operation.pFrom = str(resolved) + "\0"
    operation.pTo = None
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted:
        raise AdapterError(f"无法将文件移入 Windows 回收站（错误码 {result}）：{resolved}")


def sanitize_filename(value: str, max_length: int = 180) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "未命名")
    cleaned = cleaned.rstrip(" .") or "未命名"
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    if cleaned.upper() in reserved:
        cleaned = "_" + cleaned
    return cleaned[:max_length].rstrip(" .") or "未命名"


class ObsidianAdapter(NoteAdapter):
    endpoint = Endpoint.OBSIDIAN

    def __init__(self, config: AppConfig):
        self.config = config
        self.vault = Path(config.obsidian_vault_path).expanduser().resolve()
        self.attachment_folder_setting = self._read_attachment_folder_setting()
        self._attachment_directory_names = self._build_attachment_directory_names()
        self._filename_index: Optional[Dict[str, List[Path]]] = None

    def test_connection(self) -> str:
        if not self.vault.is_dir():
            raise AdapterError(f"Obsidian Vault 不存在：{self.vault}")
        return "Obsidian Vault 连接成功"

    def _read_attachment_folder_setting(self) -> str:
        app_config = self.vault / ".obsidian" / "app.json"
        try:
            payload = json.loads(app_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            payload = {}
        configured = payload.get("attachmentFolderPath") if isinstance(payload, dict) else None
        if isinstance(configured, str) and configured.strip():
            return configured.strip().replace("\\", "/")
        return (self.config.obsidian_attachments_folder or "attachments").strip().replace("\\", "/")

    def _build_attachment_directory_names(self) -> set[str]:
        names = {"assets", "attachments"}
        for value in (self.attachment_folder_setting, self.config.obsidian_attachments_folder):
            for part in str(value or "").replace("\\", "/").split("/"):
                cleaned = part.strip().casefold()
                if cleaned and cleaned not in {".", ".."}:
                    names.add(cleaned)
        return names

    def _walk_notes(self):
        excluded = {".obsidian", ".trash", LEGACY_TRASH_FOLDER}
        for root, directories, files in os.walk(self.vault, followlinks=False):
            directories[:] = [
                name
                for name in directories
                if name.casefold() not in excluded
                and name.casefold() not in self._attachment_directory_names
            ]
            yield Path(root), directories, files

    def _walk_resource_files(self):
        excluded = {".obsidian", ".trash", LEGACY_TRASH_FOLDER}
        for root, directories, files in os.walk(self.vault, followlinks=False):
            directories[:] = [name for name in directories if name.casefold() not in excluded]
            root_path = Path(root)
            for name in files:
                path = root_path / name
                if not path.is_symlink() and path.suffix.casefold() != ".md":
                    yield path

    def _resource_filename_index(self) -> Dict[str, List[Path]]:
        if self._filename_index is None:
            index: Dict[str, List[Path]] = {}
            for path in self._walk_resource_files():
                index.setdefault(path.name.casefold(), []).append(path)
            self._filename_index = index
        return self._filename_index

    def list_folders(self) -> List[str]:
        folders = {""}
        for root, directories, _files in self._walk_notes():
            for name in directories:
                folders.add(normalize_folder((root / name).relative_to(self.vault).as_posix()))
        return sorted(folders, key=str.casefold)

    def _resolve_attachment_path(self, target: str, note_path: Path) -> Tuple[Optional[Path], Optional[str]]:
        placeholder = type("Reference", (), {"target": target})
        normalized = normalized_local_target(placeholder)  # type: ignore[arg-type]
        if not normalized:
            return None, None
        normalized = normalized.replace("/", os.sep)
        is_absolute = bool(WINDOWS_ABSOLUTE_RE.match(normalized) or Path(normalized).is_absolute())
        if is_absolute:
            candidates = [Path(normalized)]
        elif str(target).lstrip().startswith(("/", "\\")):
            candidates = [self.vault / normalized.lstrip("/\\")]
        else:
            candidates = [note_path.parent / normalized, self.vault / normalized]
            setting = self.attachment_folder_setting
            if setting.startswith("./"):
                subfolder = setting[2:].strip("/\\")
                if subfolder:
                    candidates.append(note_path.parent / subfolder / Path(normalized).name)
            elif setting not in {".", "./"}:
                candidates.append(self.vault / setting.strip("/\\") / Path(normalized).name)

        checked = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(self.vault)
            except (OSError, ValueError):
                continue
            key = str(resolved).casefold()
            if key in checked:
                continue
            checked.add(key)
            if resolved.is_file() and resolved.suffix.casefold() != ".md":
                return resolved, None

        matches = self._resource_filename_index().get(Path(normalized).name.casefold(), [])
        if len(matches) == 1:
            return matches[0].resolve(), None
        if len(matches) > 1:
            return None, f"附件名称不唯一，找到 {len(matches)} 个同名文件：{target}"
        if is_absolute:
            return None, f"不允许同步 Vault 外部或不存在的附件：{target}"
        return None, f"找不到附件：{target}"

    def _analyze_attachments(
        self,
        note_path: Path,
        content: str,
    ) -> Tuple[List[ResolvedAttachment], List[AttachmentIssue]]:
        resolved: List[ResolvedAttachment] = []
        issues: List[AttachmentIssue] = []
        for reference in attachment_references(content):
            path, problem = self._resolve_attachment_path(reference.target, note_path)
            if path:
                resolved.append(ResolvedAttachment(reference, path))
            elif problem:
                issues.append(AttachmentIssue(reference, problem))
        return resolved, issues

    def list_notes(self) -> List[Note]:
        self._filename_index = None
        notes: List[Note] = []
        for root, _directories, files in self._walk_notes():
            for name in files:
                if not name.casefold().endswith(".md"):
                    continue
                path = root / name
                if path.is_symlink():
                    continue
                try:
                    raw_body = path.read_text(encoding="utf-8")
                    stat = path.stat()
                except (OSError, UnicodeError) as exc:
                    raise AdapterError(f"无法读取 Obsidian 文件：{path}（{exc}）") from exc
                metadata = extract_obsidian_metadata(raw_body)
                clean_body = strip_obsidian_metadata(raw_body)
                resolved, issues = self._analyze_attachments(path, clean_body)
                assets: Dict[str, Asset] = {}
                replacements = []
                revision_parts = [str(stat.st_mtime_ns), str(stat.st_size)]
                for item in resolved:
                    try:
                        data = item.path.read_bytes()
                        attachment_stat = item.path.stat()
                    except OSError as exc:
                        issues.append(AttachmentIssue(item.reference, f"无法读取附件：{item.path}（{exc}）"))
                        continue
                    digest = bytes_sha256(data)
                    assets.setdefault(
                        digest,
                        Asset(
                            digest=digest,
                            filename=item.path.name,
                            size=len(data),
                            source_ref=str(item.path),
                            _data=data,
                        ),
                    )
                    replacements.append(
                        (item.reference, canonical_asset_uri(digest, item.path.name), item.path.name)
                    )
                    revision_parts.append(f"{item.path}:{attachment_stat.st_mtime_ns}:{attachment_stat.st_size}:{digest}")
                canonical_body = replace_reference_targets(clean_body, replacements)
                relative = path.relative_to(self.vault).as_posix()
                parent = path.parent.relative_to(self.vault).as_posix()
                folder = "" if parent == "." else normalize_folder(parent)
                revision = hashlib.sha256("|".join(sorted(revision_parts)).encode("utf-8")).hexdigest()
                notes.append(
                    Note(
                        endpoint=self.endpoint,
                        native_id=relative,
                        global_id=metadata.global_id if metadata else "",
                        title=path.stem,
                        folder=folder,
                        body=canonical_body,
                        tags=tuple(extract_obsidian_tags(raw_body)),
                        updated=int(stat.st_mtime_ns // 1_000_000),
                        revision=revision,
                        locator=relative,
                        assets=assets,
                        native={
                            "path": path,
                            "raw_body": raw_body,
                            "metadata_needs_repair": obsidian_metadata_needs_repair(raw_body),
                            "attachment_issues": [issue.message for issue in issues],
                        },
                    )
                )
        return notes

    def _target_path(self, folder: str, title: str) -> Path:
        normalized = self.normalize_target_folder(folder)
        directory = self.vault if not normalized else self.vault.joinpath(*normalized.split("/"))
        return directory / f"{sanitize_filename(title)}.md"

    def normalize_target_folder(self, folder: str) -> str:
        normalized = normalize_folder(folder)
        return normalize_folder("/".join(sanitize_filename(part) for part in normalized.split("/") if part))

    def _resolve_target_path(
        self,
        folder: str,
        title: str,
        global_id: str,
        existing_path: Optional[Path],
    ) -> Path:
        desired = self._target_path(folder, title)
        if not desired.exists() or (existing_path and desired.resolve() == existing_path.resolve()):
            return desired
        try:
            metadata = extract_obsidian_metadata(desired.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            metadata = None
        if metadata and metadata.global_id == global_id:
            return desired
        return desired.with_name(f"{desired.stem}_{global_id[:8]}{desired.suffix}")

    def _atomic_write(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".notesynchub.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()

    def _attachment_destination(self, note_path: Path) -> Path:
        setting = self.attachment_folder_setting.strip().replace("\\", "/")
        if setting in {".", "./"}:
            folder = note_path.parent
        elif setting.startswith("./"):
            folder = note_path.parent.joinpath(*[part for part in setting[2:].split("/") if part])
        else:
            folder = self.vault.joinpath(*[part for part in setting.strip("/").split("/") if part])
        try:
            resolved = folder.resolve()
            resolved.relative_to(self.vault)
            return resolved
        except (OSError, ValueError):
            return note_path.parent / "assets"

    @staticmethod
    def _attachment_link(note_path: Path, attachment_path: Path) -> str:
        relative = os.path.relpath(str(attachment_path), str(note_path.parent)).replace("\\", "/")
        return quote(relative, safe="/@-._~")

    def _write_attachment(self, note_path: Path, asset: Asset) -> str:
        data = asset.load()
        folder = self._attachment_destination(note_path)
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = sanitize_filename(asset.filename, max_length=200)
        safe_path = Path(safe_name)
        path = folder / safe_name
        if path.is_file() and path.read_bytes() == data:
            return self._attachment_link(note_path, path)
        if path.exists():
            path = folder / f"{path.stem}_{asset.digest[:8]}{path.suffix}"
            counter = 2
            while path.exists() and (not path.is_file() or path.read_bytes() != data):
                path = folder / f"{safe_path.stem}_{asset.digest[:8]}_{counter}{safe_path.suffix}"
                counter += 1
            if path.is_file() and path.read_bytes() == data:
                return self._attachment_link(note_path, path)
        temporary = path.with_name(path.name + ".notesynchub.tmp")
        try:
            temporary.write_bytes(data)
            os.replace(str(temporary), str(path))
        finally:
            if temporary.exists():
                temporary.unlink()
        self._filename_index = None
        return self._attachment_link(note_path, path)

    def upsert_note(self, source: Note, existing: Optional[Note], folder: str, global_id: str) -> str:
        existing_path = Path(existing.native["path"]) if existing and existing.native.get("path") else None
        target = self._resolve_target_path(folder, source.title, global_id, existing_path)
        targets: Dict[str, str] = {}
        existing_assets = existing.assets if existing else {}
        for digest, asset in source.assets.items():
            previous = existing_assets.get(digest)
            previous_path = Path(previous.source_ref) if previous and previous.source_ref else None
            if previous_path and previous_path.is_file():
                try:
                    previous_data = previous_path.read_bytes()
                except OSError:
                    previous_data = b""
                if bytes_sha256(previous_data) == digest:
                    targets[digest] = self._attachment_link(target, previous_path)
                    continue
            targets[digest] = self._write_attachment(target, asset)
        body = replace_canonical_asset_uris(source.body, targets)
        content = apply_obsidian_metadata(
            body,
            SyncMetadata.create(source.endpoint.value, global_id),
            source.tags,
        )
        self._atomic_write(target, content)
        if existing_path and existing_path.exists() and existing_path.resolve() != target.resolve():
            send_to_windows_recycle_bin(existing_path)
        return target.relative_to(self.vault).as_posix()

    def set_global_id(self, note: Note, global_id: str) -> None:
        path = Path(note.native.get("path", self.vault / note.native_id))
        raw_body = str(note.native.get("raw_body", "") or path.read_text(encoding="utf-8"))
        content = apply_obsidian_metadata(
            raw_body,
            SyncMetadata.create(note.endpoint.value, global_id),
            note.tags,
        )
        self._atomic_write(path, content)

    def move_to_trash(self, note: Note) -> None:
        path = Path(note.native.get("path", self.vault / note.native_id))
        send_to_windows_recycle_bin(path)
