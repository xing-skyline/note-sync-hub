from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .adapters import JoplinAdapter, NoteAdapter, ObsidianAdapter, SiYuanAdapter
from .adapters.base import AdapterError
from .config import AppConfig
from .models import (
    ConflictPolicy,
    Endpoint,
    ExecutionResult,
    Note,
    OperationAction,
    SyncMode,
    SyncOperation,
    SyncOptions,
    SyncPlan,
    TargetMode,
    normalize_folder,
)
from .state import StateStore


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str], None]


class SyncEngineError(RuntimeError):
    pass


class SyncEngine:
    """只读规划与显式执行分离的三端同步核心。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        adapters: Optional[Dict[Endpoint, NoteAdapter]] = None,
        state_store: Optional[StateStore] = None,
        logger: Optional[LogCallback] = None,
    ):
        self.config = config
        self.adapters: Dict[Endpoint, NoteAdapter] = adapters or {
            Endpoint.JOPLIN: JoplinAdapter(config),
            Endpoint.OBSIDIAN: ObsidianAdapter(config),
            Endpoint.SIYUAN: SiYuanAdapter(config),
        }
        self.state_store = state_store or StateStore(config.state_path())
        self.log = logger or (lambda _message: None)

    def test_connections(self, endpoints: Iterable[Endpoint]) -> Dict[Endpoint, str]:
        selected = tuple(dict.fromkeys(endpoints))
        self.config.validate(selected)
        results: Dict[Endpoint, str] = {}
        for endpoint in selected:
            results[endpoint] = self.adapters[endpoint].test_connection()
        return results

    def discover_folders(self, endpoints: Iterable[Endpoint]) -> Dict[Endpoint, List[str]]:
        selected = tuple(dict.fromkeys(endpoints))
        self.config.validate(selected)
        result: Dict[Endpoint, List[str]] = {}
        for endpoint in selected:
            self.log(f"正在读取 {endpoint.label} 的目录结构……")
            result[endpoint] = self.adapters[endpoint].list_folders()
        return result

    def scan(self, endpoints: Iterable[Endpoint]) -> Dict[Endpoint, List[Note]]:
        selected = tuple(dict.fromkeys(endpoints))
        self.config.validate(selected)
        result: Dict[Endpoint, List[Note]] = {}
        for endpoint in selected:
            self.log(f"正在扫描 {endpoint.label}……")
            result[endpoint] = self.adapters[endpoint].list_notes()
            self.log(f"{endpoint.label} 扫描完成：{len(result[endpoint])} 条笔记。")
        return result

    @staticmethod
    def _fingerprints(notes: Dict[Endpoint, List[Note]]) -> Dict[Endpoint, Dict[str, str]]:
        return {
            endpoint: {note.native_id: note.snapshot_key for note in endpoint_notes}
            for endpoint, endpoint_notes in notes.items()
        }

    @staticmethod
    def _record_endpoints(record: Optional[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
        if not isinstance(record, dict):
            return {}
        endpoints = record.get("endpoints", {})
        return endpoints if isinstance(endpoints, dict) else {}  # type: ignore[return-value]

    @staticmethod
    def _record_for(record: Optional[Dict[str, object]], endpoint: Endpoint) -> Dict[str, object]:
        value = SyncEngine._record_endpoints(record).get(endpoint.value, {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _record_changed(note: Note, record: Dict[str, object]) -> bool:
        if not record:
            return True
        return any(
            (
                str(record.get("signature", "")) != note.content_signature,
                str(record.get("title", "")) != note.title,
                normalize_folder(str(record.get("folder", ""))) != note.folder,
            )
        )

    @staticmethod
    def _record_path_changed(note: Note, record: Dict[str, object]) -> bool:
        if not record:
            return False
        return (
            str(record.get("title", "")) != note.title
            or normalize_folder(str(record.get("folder", ""))) != note.folder
        )

    @staticmethod
    def _record_in_scope(options: SyncOptions, endpoint: Endpoint, record: Dict[str, object]) -> bool:
        if not record:
            return False
        folder = normalize_folder(str(record.get("folder", "")))
        if options.includes(endpoint, folder):
            return True
        if endpoint != Endpoint.SIYUAN or options.scope_all:
            return False
        full_path = normalize_folder("/".join(part for part in (folder, str(record.get("title", ""))) if part))
        for selected in options.selected_folders.get(endpoint, ()):
            root = normalize_folder(selected)
            if full_path == root or (options.include_subfolders and root and full_path.startswith(root + "/")):
                return True
        return False

    def _mapped_one_way_folder(self, options: SyncOptions, source: Note, target: Endpoint) -> str:
        if options.target_mode == TargetMode.PRESERVE:
            raw_folder = source.folder
        elif source.endpoint == Endpoint.SIYUAN:
            full_path = normalize_folder("/".join(part for part in (source.folder, source.title) if part))
            selected = {normalize_folder(value) for value in options.selected_folders.get(source.endpoint, ())}
            if full_path in selected:
                if options.target_mode == TargetMode.ROOT:
                    raw_folder = ""
                else:
                    raw_folder = normalize_folder(options.target_folders.get(target, ""))
            else:
                raw_folder = options.mapped_target_folder(source.folder, target)
        else:
            raw_folder = options.mapped_target_folder(source.folder, target)
        return self.adapters[target].normalize_target_folder(raw_folder)

    def _desired_target_folder(
        self,
        options: SyncOptions,
        source: Note,
        target: Endpoint,
        existing: Optional[Note],
        state_record: Optional[Dict[str, object]],
    ) -> str:
        if options.mode == SyncMode.ONE_WAY:
            return self._mapped_one_way_folder(options, source, target)
        if existing is None:
            return self.adapters[target].normalize_target_folder(source.folder)

        source_record = self._record_for(state_record, source.endpoint)
        target_record = self._record_for(state_record, target)
        if not source_record or not self._record_path_changed(source, source_record):
            return self.adapters[target].normalize_target_folder(existing.folder)

        old_source = normalize_folder(str(source_record.get("folder", "")))
        old_target = normalize_folder(str(target_record.get("folder", existing.folder)))
        new_source = source.folder
        if new_source == old_source:
            return self.adapters[target].normalize_target_folder(existing.folder)
        if old_source and new_source.startswith(old_source + "/"):
            suffix = new_source[len(old_source) :].strip("/")
            translated = normalize_folder("/".join(part for part in (old_target, suffix) if part))
            return self.adapters[target].normalize_target_folder(translated)
        if not old_source and new_source:
            translated = normalize_folder("/".join(part for part in (old_target, new_source) if part))
            return self.adapters[target].normalize_target_folder(translated)
        return self.adapters[target].normalize_target_folder(new_source)

    def _expected_path_key(self, source: Note, folder: str, target: Endpoint) -> str:
        title = self.adapters[target].normalize_target_title(source.title)
        return normalize_folder("/".join(part for part in (folder, title) if part)).casefold()

    def _build_groups(
        self,
        notes: Dict[Endpoint, List[Note]],
        previous: Dict[str, object],
        options: SyncOptions,
    ) -> Tuple[List[Tuple[str, Dict[Endpoint, Note], Optional[Dict[str, object]]]], List[SyncOperation]]:
        groups: Dict[str, Dict[Endpoint, Note]] = defaultdict(dict)
        records: Dict[str, Optional[Dict[str, object]]] = {}
        used: Dict[Endpoint, Set[str]] = {endpoint: set() for endpoint in options.endpoints}
        diagnostics: List[SyncOperation] = []

        by_global: Dict[Endpoint, Dict[str, List[Note]]] = {}
        for endpoint in options.endpoints:
            endpoint_map: Dict[str, List[Note]] = defaultdict(list)
            for note in notes.get(endpoint, []):
                if note.global_id:
                    endpoint_map[note.global_id].append(note)
            by_global[endpoint] = endpoint_map

        # 旧版思源扫描被接口默认截断后，可能在来源的新路径创建了副本，而旧
        # 路径副本仍保留相同同步 ID。单向同步有明确来源和目标路径，只有当
        # 重复副本中恰好一个位于当前期望路径时才安全选中；其他情况仍按冲突
        # 处理，且这里不会删除或改写未选中的旧副本。
        if options.mode == SyncMode.ONE_WAY and options.source is not None:
            source_endpoint = options.source
            for target in options.targets:
                for global_id, candidates in list(by_global[target].items()):
                    if len(candidates) <= 1:
                        continue
                    sources = by_global[source_endpoint].get(global_id, [])
                    if len(sources) != 1 or not options.includes_note(sources[0]):
                        continue
                    folder = self._mapped_one_way_folder(options, sources[0], target)
                    expected_path = self._expected_path_key(sources[0], folder, target)
                    exact = [note for note in candidates if note.path_key == expected_path]
                    if len(exact) == 1:
                        used[target].update(
                            note.native_id
                            for note in candidates
                            if note.native_id != exact[0].native_id
                        )
                        by_global[target][global_id] = exact

        duplicate_ids: Set[Tuple[Endpoint, str]] = set()
        for endpoint, endpoint_map in by_global.items():
            for global_id, candidates in endpoint_map.items():
                if len(candidates) <= 1:
                    continue
                duplicate_ids.add((endpoint, global_id))
                used[endpoint].update(note.native_id for note in candidates)
                diagnostics.append(
                    SyncOperation(
                        global_id=global_id,
                        action=OperationAction.CONFLICT,
                        title=candidates[0].title,
                        versions={endpoint: candidates[0]},
                        reason=f"{endpoint.label} 中同步 ID {global_id[:8]}… 重复了 {len(candidates)} 次，请先人工处理。",
                    )
                )

        invalid_global_ids = {global_id for _endpoint, global_id in duplicate_ids}
        for global_id in invalid_global_ids:
            for endpoint in options.endpoints:
                used[endpoint].update(note.native_id for note in by_global[endpoint].get(global_id, []))

        global_ids = set()
        for endpoint_map in by_global.values():
            global_ids.update(endpoint_map)
        for global_id in sorted(global_ids):
            if global_id in invalid_global_ids:
                continue
            for endpoint in options.endpoints:
                if (endpoint, global_id) in duplicate_ids:
                    continue
                candidates = by_global[endpoint].get(global_id, [])
                if candidates:
                    groups[global_id][endpoint] = candidates[0]
                    used[endpoint].add(candidates[0].native_id)
            if groups.get(global_id):
                record = previous.get(global_id)
                records[global_id] = record if isinstance(record, dict) else None

        # 同步标记丢失时，用上次状态中的原生 ID 恢复关联。
        native_indexes = {
            endpoint: {note.native_id: note for note in notes.get(endpoint, [])}
            for endpoint in options.endpoints
        }
        for global_id, raw_record in previous.items():
            if global_id in invalid_global_ids:
                continue
            if not isinstance(raw_record, dict):
                continue
            record = raw_record
            for endpoint in options.endpoints:
                endpoint_record = self._record_for(record, endpoint)
                native_id = str(endpoint_record.get("native_id", ""))
                note = native_indexes[endpoint].get(native_id)
                if not note or note.native_id in used[endpoint]:
                    continue
                if endpoint in groups[global_id]:
                    diagnostics.append(
                        SyncOperation(
                            global_id=global_id,
                            action=OperationAction.CONFLICT,
                            title=note.title,
                            versions={endpoint: note},
                            reason=f"{endpoint.label} 的同步标记与历史状态指向两条不同笔记。",
                        )
                    )
                    used[endpoint].add(note.native_id)
                    continue
                groups[global_id][endpoint] = note
                used[endpoint].add(note.native_id)
            if groups.get(global_id):
                records[global_id] = record

        if options.mode == SyncMode.ONE_WAY and options.source is not None:
            source_endpoint = options.source
            target_indexes: Dict[Endpoint, Dict[str, List[Note]]] = {}
            for target in options.targets:
                index: Dict[str, List[Note]] = defaultdict(list)
                for note in notes.get(target, []):
                    index[note.path_key].append(note)
                target_indexes[target] = index

            target_owners: Dict[Endpoint, Dict[str, str]] = {
                target: {
                    note.native_id: global_id
                    for global_id, versions in groups.items()
                    if (note := versions.get(target)) is not None
                }
                for target in options.targets
            }

            # 来源已带同步 ID、但目标写入曾失败时，目标端的同路径未关联笔记
            # 仍应重新并入原组。目标若带有一个已经没有来源副本的旧同步 ID，
            # 也视为可安全接管的孤立副本；若旧组仍有来源笔记则不抢占。
            for global_id, versions in list(groups.items()):
                source = versions.get(source_endpoint)
                if source is None or not options.includes_note(source):
                    continue
                for target in options.targets:
                    if target in versions:
                        continue
                    folder = self._mapped_one_way_folder(options, source, target)
                    candidates = target_indexes[target].get(self._expected_path_key(source, folder, target), [])
                    if len(candidates) != 1:
                        continue
                    candidate = candidates[0]
                    owner_id = target_owners[target].get(candidate.native_id, "")
                    if owner_id and owner_id != global_id:
                        owner_versions = groups.get(owner_id)
                        owner_source = owner_versions.get(source_endpoint) if owner_versions else None
                        if owner_source is not None and owner_source.native_id != source.native_id:
                            continue
                        if owner_versions is not None:
                            owner_versions.pop(target, None)
                            if not owner_versions:
                                groups.pop(owner_id, None)
                                records.pop(owner_id, None)
                    versions[target] = candidate
                    used[target].add(candidate.native_id)
                    target_owners[target][candidate.native_id] = global_id

            for source in notes.get(source_endpoint, []):
                if source.native_id in used[source_endpoint] or not options.includes_note(source):
                    continue
                versions: Dict[Endpoint, Note] = {source_endpoint: source}
                ambiguous = False
                for target in options.targets:
                    folder = self._mapped_one_way_folder(options, source, target)
                    candidates = target_indexes[target].get(self._expected_path_key(source, folder, target), [])
                    candidates = [note for note in candidates if note.native_id not in used[target]]
                    if len(candidates) > 1:
                        ambiguous = True
                        used[target].update(note.native_id for note in candidates)
                        diagnostics.append(
                            SyncOperation(
                                global_id="",
                                action=OperationAction.CONFLICT,
                                title=source.title,
                                versions={source_endpoint: source, target: candidates[0]},
                                reason=f"{target.label} 的目标路径存在 {len(candidates)} 条候选笔记，无法安全配对。",
                            )
                        )
                    elif candidates:
                        versions[target] = candidates[0]
                        used[target].add(candidates[0].native_id)
                used[source_endpoint].add(source.native_id)
                if ambiguous:
                    continue
                global_id = next((note.global_id for note in versions.values() if note.global_id), "") or str(uuid.uuid4())
                groups[global_id] = versions
                records[global_id] = previous.get(global_id) if isinstance(previous.get(global_id), dict) else None
        else:
            path_buckets: Dict[str, Dict[Endpoint, List[Note]]] = defaultdict(lambda: defaultdict(list))
            for endpoint in options.endpoints:
                for note in notes.get(endpoint, []):
                    if note.native_id not in used[endpoint]:
                        path_buckets[note.path_key][endpoint].append(note)
            for _path, endpoint_candidates in sorted(path_buckets.items()):
                flat = [note for candidates in endpoint_candidates.values() for note in candidates]
                if not any(options.includes_note(note) for note in flat):
                    continue
                duplicates = {endpoint: values for endpoint, values in endpoint_candidates.items() if len(values) > 1}
                if duplicates:
                    for endpoint, values in duplicates.items():
                        used[endpoint].update(note.native_id for note in values)
                    first = flat[0]
                    diagnostics.append(
                        SyncOperation(
                            global_id="",
                            action=OperationAction.CONFLICT,
                            title=first.title,
                            versions={endpoint: values[0] for endpoint, values in endpoint_candidates.items()},
                            reason="同一路径在至少一个笔记端对应多条笔记，无法安全自动配对。",
                        )
                    )
                    continue
                versions = {endpoint: values[0] for endpoint, values in endpoint_candidates.items()}
                for endpoint, note in versions.items():
                    used[endpoint].add(note.native_id)
                global_id = next((note.global_id for note in versions.values() if note.global_id), "") or str(uuid.uuid4())
                groups[global_id] = versions
                records[global_id] = previous.get(global_id) if isinstance(previous.get(global_id), dict) else None

        # 只在历史状态中存在、且当前至少仍有一端存在的组也需要规划删除。
        for global_id, raw_record in previous.items():
            if global_id in invalid_global_ids or global_id in groups or not isinstance(raw_record, dict):
                continue
            versions: Dict[Endpoint, Note] = {}
            for endpoint in options.endpoints:
                endpoint_record = self._record_for(raw_record, endpoint)
                native_id = str(endpoint_record.get("native_id", ""))
                note = native_indexes[endpoint].get(native_id)
                if note and note.native_id not in used[endpoint]:
                    versions[endpoint] = note
                    used[endpoint].add(note.native_id)
            if versions:
                groups[global_id] = versions
                records[global_id] = raw_record

        ordered = [
            (global_id, versions, records.get(global_id))
            for global_id, versions in sorted(groups.items(), key=lambda item: item[0])
        ]
        return ordered, diagnostics

    @staticmethod
    def _attachment_problem(notes: Iterable[Note]) -> str:
        problems: List[str] = []
        for note in notes:
            issues = note.native.get("attachment_issues", [])
            if isinstance(issues, list):
                problems.extend(str(issue) for issue in issues if issue)
        return "；".join(problems)

    def _plan_one_way(
        self,
        global_id: str,
        versions: Dict[Endpoint, Note],
        record: Optional[Dict[str, object]],
        options: SyncOptions,
    ) -> List[SyncOperation]:
        source_endpoint = options.source
        if source_endpoint is None:
            return []
        source = versions.get(source_endpoint)
        source_record = self._record_for(record, source_endpoint)

        if source is None:
            if not source_record or not self._record_in_scope(options, source_endpoint, source_record):
                return []
            existing_targets = tuple(target for target in options.targets if target in versions)
            if not existing_targets:
                return []
            if not options.propagate_deletions:
                return [
                    SyncOperation(
                        global_id=global_id,
                        action=OperationAction.SKIP,
                        title=str(source_record.get("title", "已删除笔记")),
                        versions=versions,
                        targets=existing_targets,
                        reason="来源端笔记已删除；“传播删除”未开启，因此目标端保留不动。",
                        state_record=record,
                    )
                ]
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.DELETE,
                    title=str(source_record.get("title", "已删除笔记")),
                    versions=versions,
                    targets=existing_targets,
                    reason=(
                        "来源端删除将传播到目标端；Joplin 使用废纸篓，Obsidian 使用 Windows 回收站，"
                        "思源移入统一的 Note Sync Hub 回收站。"
                    ),
                    state_record=record,
                )
            ]

        if not options.includes_note(source):
            return []
        attachment_problem = self._attachment_problem([source])
        if attachment_problem:
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.CONFLICT,
                    title=source.title,
                    versions=versions,
                    reason=f"来源笔记存在无法确认的附件，已停止自动覆盖：{attachment_problem}",
                    state_record=record,
                )
            ]

        target_folders = {
            target: self._desired_target_folder(options, source, target, versions.get(target), record)
            for target in options.targets
        }
        creates: List[Endpoint] = []
        content_updates: List[Endpoint] = []
        moves: List[Endpoint] = []
        needs_link = (
            source.global_id != global_id
            or bool(source.native.get("metadata_needs_repair"))
        )
        for target in options.targets:
            current = versions.get(target)
            if current is None:
                creates.append(target)
                continue
            needs_link = (
                needs_link
                or current.global_id != global_id
                or bool(current.native.get("metadata_needs_repair"))
            )
            target_title = self.adapters[target].normalize_target_title(source.title)
            source_unchanged = not self._record_changed(source, source_record)
            target_record = self._record_for(record, target)
            target_unchanged = not self._record_changed(current, target_record)
            equivalent_baseline = bool(source_record and target_record and source_unchanged and target_unchanged)
            if not equivalent_baseline and (
                current.content_signature != source.content_signature or current.title != target_title
            ):
                content_updates.append(target)
            elif current.folder != target_folders[target]:
                moves.append(target)

        changed_targets = tuple(dict.fromkeys([*creates, *content_updates, *moves]))
        if changed_targets:
            if content_updates:
                action = OperationAction.UPDATE
            elif moves:
                action = OperationAction.MOVE
            else:
                action = OperationAction.CREATE
            details = []
            if creates:
                details.append("新建：" + "、".join(item.label for item in creates))
            if content_updates:
                details.append("更新：" + "、".join(item.label for item in content_updates))
            if moves:
                details.append("移动：" + "、".join(item.label for item in moves))
            return [
                SyncOperation(
                    global_id=global_id,
                    action=action,
                    title=source.title,
                    versions=versions,
                    source=source_endpoint,
                    targets=changed_targets,
                    target_folders={target: target_folders[target] for target in changed_targets},
                    reason="；".join(details),
                    state_record=record,
                )
            ]
        if needs_link:
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.LINK,
                    title=source.title,
                    versions=versions,
                    reason="内容一致，只补写同步标记，不覆盖正文。",
                    state_record=record,
                )
            ]
        return []

    def _plan_bidirectional(
        self,
        global_id: str,
        versions: Dict[Endpoint, Note],
        record: Optional[Dict[str, object]],
        options: SyncOptions,
    ) -> List[SyncOperation]:
        primary = options.primary
        if primary is None:
            raise SyncEngineError("双向同步缺少主端。")

        def preferred_version(candidates: Iterable[Endpoint]) -> Tuple[Endpoint, Note]:
            available = [endpoint for endpoint in candidates if endpoint in versions]
            if primary in available:
                return primary, versions[primary]
            endpoint = max(available, key=lambda item: (versions[item].updated, item.value))
            return endpoint, versions[endpoint]

        def conflict_or_latest(reason: str) -> List[SyncOperation]:
            if options.conflict_policy == ConflictPolicy.LATEST:
                latest_updated = max((note.updated for note in versions.values()), default=0)
                latest = [
                    (endpoint, note)
                    for endpoint, note in versions.items()
                    if note.updated == latest_updated
                ]
                if latest_updated > 0 and len(latest) == 1:
                    source_endpoint, source = latest[0]
                    targets = tuple(endpoint for endpoint in options.endpoints if endpoint != source_endpoint)
                    target_folders = {
                        target: self._desired_target_folder(
                            options,
                            source,
                            target,
                            versions.get(target),
                            record,
                        )
                        for target in targets
                    }
                    try:
                        updated_label = datetime.fromtimestamp(
                            latest_updated / 1000,
                            tz=timezone.utc,
                        ).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                    except (OSError, OverflowError, ValueError):
                        updated_label = str(latest_updated)
                    action = (
                        OperationAction.CREATE
                        if all(target not in versions for target in targets)
                        else OperationAction.UPDATE
                    )
                    return [
                        SyncOperation(
                            global_id=global_id,
                            action=action,
                            title=source.title,
                            versions=versions,
                            source=source_endpoint,
                            targets=targets,
                            target_folders=target_folders,
                            reason=(
                                f"{reason} 已按“自动采用最新版本”选择 {source_endpoint.label}："
                                f"最后修改时间 {updated_label}。该操作仍需在预览中确认后执行。"
                            ),
                            state_record=record,
                        )
                    ]
                reason += (
                    " 已开启自动最新策略，但两端修改时间相同或不可用，仍需人工比较；"
                    f"{primary.label} 作为默认参考。"
                )
            else:
                reason += f" 请人工比较并选择保留版本；{primary.label} 作为默认参考。"
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.CONFLICT,
                    title=next(iter(versions.values())).title,
                    versions=versions,
                    reason=reason,
                    state_record=record,
                )
            ]

        endpoint_records = {endpoint: self._record_for(record, endpoint) for endpoint in options.endpoints}
        if not any(options.includes_note(note) for note in versions.values()) and not any(
            self._record_in_scope(options, endpoint, endpoint_records[endpoint])
            for endpoint in options.endpoints
        ):
            return []

        attachment_problem = self._attachment_problem(versions.values())
        if attachment_problem:
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.CONFLICT,
                    title=next(iter(versions.values())).title,
                    versions=versions,
                    reason=(
                        f"至少一端存在无法确认的附件，已停止自动覆盖：{attachment_problem}。"
                        f"双向主端为 {primary.label}。"
                    ),
                    state_record=record,
                )
            ]

        deleted = [
            endpoint
            for endpoint in options.endpoints
            if endpoint_records[endpoint] and endpoint not in versions
        ]
        never_created = [
            endpoint
            for endpoint in options.endpoints
            if not endpoint_records[endpoint] and endpoint not in versions
        ]
        changed = [
            endpoint
            for endpoint, note in versions.items()
            if endpoint_records[endpoint] and self._record_changed(note, endpoint_records[endpoint])
        ]
        newly_present = [
            endpoint
            for endpoint in versions
            if not endpoint_records[endpoint]
        ]
        primary_deleted = primary in deleted
        secondary_deleted = [endpoint for endpoint in deleted if endpoint != primary]
        missing_targets = list(dict.fromkeys([*secondary_deleted, *never_created]))

        if primary_deleted:
            if not versions:
                return []
            if changed:
                return [
                    SyncOperation(
                        global_id=global_id,
                        action=OperationAction.CONFLICT,
                        title=next(iter(versions.values())).title,
                        versions=versions,
                        reason=(
                            f"主端 {primary.label} 已删除，同时其他端又有修改：修改端为 "
                            + "、".join(item.label for item in changed)
                            + "。为避免数据丢失，不会自动删除；可在冲突窗口选择现存内容并恢复主端。"
                        ),
                        state_record=record,
                    )
                ]
            current_targets = tuple(versions)
            if not options.propagate_deletions:
                return [
                    SyncOperation(
                        global_id=global_id,
                        action=OperationAction.SKIP,
                        title=next(iter(versions.values())).title,
                        versions=versions,
                        targets=current_targets,
                        reason=(
                            f"检测到主端 {primary.label} 删除；“将主端删除同步到其他端”未开启，"
                            "其他端全部保留。"
                        ),
                        state_record=record,
                    )
                ]
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.DELETE,
                    title=next(iter(versions.values())).title,
                    versions=versions,
                    targets=current_targets,
                    reason=(
                        f"主端 {primary.label} 的删除将传播到仍存在的目标端；"
                        "Joplin 使用废纸篓，Obsidian 使用 Windows 回收站，"
                        "思源移入统一的 Note Sync Hub 回收站。"
                    ),
                    state_record=record,
                )
            ]

        if not record:
            if not versions:
                return []
            unique_contents = {note.content_signature for note in versions.values()}
            unique_titles = {note.title for note in versions.values()}
            if len(versions) > 1 and (len(unique_contents) > 1 or len(unique_titles) > 1):
                return conflict_or_latest(
                    "首次配对时发现同路径内容不同。"
                )
            source_endpoint, source = preferred_version(versions)
            targets = tuple(endpoint for endpoint in options.endpoints if endpoint not in versions)
            if targets:
                return [
                    SyncOperation(
                        global_id=global_id,
                        action=OperationAction.CREATE,
                        title=source.title,
                        versions=versions,
                        source=source_endpoint,
                        targets=targets,
                        target_folders={
                            target: self.adapters[target].normalize_target_folder(source.folder)
                            for target in targets
                        },
                        reason="首次同步：在缺少此笔记的所选端新建副本。",
                    )
                ]
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.LINK,
                    title=source.title,
                    versions=versions,
                    reason="首次配对内容一致，只写入统一同步标记。",
                )
            ]

        if len(changed) > 1:
            content_variants = {versions[endpoint].content_signature for endpoint in changed}
            title_variants = {versions[endpoint].title for endpoint in changed}
            path_changes = [
                endpoint
                for endpoint in changed
                if normalize_folder(str(endpoint_records[endpoint].get("folder", "")))
                != versions[endpoint].folder
            ]
            path_variants = {versions[endpoint].folder for endpoint in path_changes}
            if len(content_variants) > 1 or len(title_variants) > 1 or len(path_variants) > 1:
                return conflict_or_latest(
                    "多个笔记端在上次同步后分别发生了不同修改。"
                )

        if not changed and newly_present:
            signatures = {note.content_signature for note in versions.values()}
            titles = {note.title for note in versions.values()}
            if len(signatures) > 1 or len(titles) > 1:
                return conflict_or_latest("一个新出现的关联副本与已有同步版本内容不同。")
            if not missing_targets:
                return [
                    SyncOperation(
                        global_id=global_id,
                        action=OperationAction.LINK,
                        title=next(iter(versions.values())).title,
                        versions=versions,
                        reason="新出现的关联副本内容一致，更新同步状态。",
                        state_record=record,
                    )
                ]

        if changed:
            source_endpoint, source = preferred_version(changed)
            incompatible_new = [
                endpoint
                for endpoint in newly_present
                if versions[endpoint].content_signature != source.content_signature
                or versions[endpoint].title != source.title
            ]
            if incompatible_new:
                return conflict_or_latest(
                    "已有端发生修改，同时新出现的关联副本内容不同。"
                )
            targets = tuple(endpoint for endpoint in options.endpoints if endpoint != source_endpoint)
            target_folders = {
                target: self._desired_target_folder(options, source, target, versions.get(target), record)
                for target in targets
            }
            relevant_targets = tuple(
                target
                for target in targets
                if target not in versions
                or versions[target].content_signature != source.content_signature
                or versions[target].title != source.title
                or versions[target].folder != target_folders[target]
            )
            if relevant_targets:
                action = OperationAction.CREATE if all(target not in versions for target in relevant_targets) else OperationAction.UPDATE
                return [
                    SyncOperation(
                        global_id=global_id,
                        action=action,
                        title=source.title,
                        versions=versions,
                        source=source_endpoint,
                        targets=relevant_targets,
                        target_folders={target: target_folders[target] for target in relevant_targets},
                        reason=(
                            f"仅 {source_endpoint.label} 在上次同步后发生变化，将传播到其他所选端。"
                            + (
                                "检测到非主端删除，缺失副本也会按主端规则恢复。"
                                if secondary_deleted
                                else ""
                            )
                        ),
                        state_record=record,
                    )
                ]
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.LINK,
                    title=source.title,
                    versions=versions,
                    reason="多个笔记端已经得到相同修改，只更新同步基线，不覆盖正文。",
                    state_record=record,
                )
            ]

        if missing_targets and versions:
            source_endpoint, source = preferred_version(versions)
            targets = tuple(missing_targets)
            if secondary_deleted:
                reference = primary.label if primary in versions else source_endpoint.label
                reason = (
                    f"非主端（{'、'.join(item.label for item in secondary_deleted)}）删除或缺失，"
                    f"将从参考端 {reference} 恢复；新加入的端同时补建副本。"
                )
            else:
                reason = "为后来加入同步范围的笔记端补建副本。"
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.CREATE,
                    title=source.title,
                    versions=versions,
                    source=source_endpoint,
                    targets=targets,
                    target_folders={
                        target: self.adapters[target].normalize_target_folder(source.folder)
                        for target in targets
                    },
                    reason=reason,
                    state_record=record,
                )
            ]

        if any(not note.global_id for note in versions.values()):
            return [
                SyncOperation(
                    global_id=global_id,
                    action=OperationAction.LINK,
                    title=next(iter(versions.values())).title,
                    versions=versions,
                    reason="正文未变化，仅修复缺失的同步标记。",
                    state_record=record,
                )
            ]
        return []

    def preview(self, options: SyncOptions) -> SyncPlan:
        options.validate()
        self.config.validate(options.endpoints)
        notes = self.scan(options.endpoints)
        state = self.state_store.load()
        previous = state.get("groups", {})
        previous_groups = previous if isinstance(previous, dict) else {}
        groups, operations = self._build_groups(notes, previous_groups, options)
        for global_id, versions, record in groups:
            if options.mode == SyncMode.ONE_WAY:
                operations.extend(self._plan_one_way(global_id, versions, record, options))
            else:
                operations.extend(self._plan_bidirectional(global_id, versions, record, options))
        operations.sort(key=lambda item: (item.title.casefold(), item.action.value, item.global_id))
        return SyncPlan(
            options=options,
            operations=operations,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            scan_fingerprints=self._fingerprints(notes),
        )

    def _verify_plan_is_fresh(self, plan: SyncPlan) -> Dict[Endpoint, List[Note]]:
        current = self.scan(plan.options.endpoints)
        if self._fingerprints(current) != plan.scan_fingerprints:
            raise SyncEngineError("预览后笔记或附件发生了变化。为防止覆盖，已停止执行，请重新生成同步预览。")
        return current

    @staticmethod
    def _snapshot_state(
        notes: Dict[Endpoint, List[Note]],
        preferred_native_ids: Optional[Dict[str, Dict[Endpoint, str]]] = None,
    ) -> Dict[str, Dict[str, object]]:
        groups: Dict[str, Dict[str, object]] = {}

        def save_note(note: Note) -> None:
            group = groups.setdefault(note.global_id, {"endpoints": {}})
            endpoints = group["endpoints"]
            if not isinstance(endpoints, dict):
                return
            endpoints[note.endpoint.value] = {
                "native_id": note.native_id,
                "title": note.title,
                "folder": note.folder,
                "signature": note.content_signature,
                "revision": note.revision,
                "updated": note.updated,
                "locator": note.locator,
            }

        for endpoint, endpoint_notes in notes.items():
            for note in endpoint_notes:
                if not note.global_id:
                    continue
                save_note(note)

        if preferred_native_ids:
            native_indexes = {
                endpoint: {note.native_id: note for note in endpoint_notes}
                for endpoint, endpoint_notes in notes.items()
            }
            for global_id, endpoint_ids in preferred_native_ids.items():
                for endpoint, native_id in endpoint_ids.items():
                    note = native_indexes.get(endpoint, {}).get(native_id)
                    if note is not None and note.global_id == global_id:
                        save_note(note)
        return groups

    @staticmethod
    def _merge_selected_snapshot(
        previous: Optional[Dict[str, object]],
        current: Optional[Dict[str, object]],
        selected: Iterable[Endpoint],
    ) -> Optional[Dict[str, object]]:
        merged = dict(previous) if isinstance(previous, dict) else {}
        endpoints = dict(SyncEngine._record_endpoints(previous))
        current_endpoints = SyncEngine._record_endpoints(current)
        for endpoint in selected:
            endpoints.pop(endpoint.value, None)
            value = current_endpoints.get(endpoint.value)
            if isinstance(value, dict):
                endpoints[endpoint.value] = value
        if not endpoints:
            return None
        merged["endpoints"] = endpoints
        return merged

    def execute(
        self,
        plan: SyncPlan,
        *,
        cancel_event: Optional[threading.Event] = None,
        progress: Optional[ProgressCallback] = None,
    ) -> ExecutionResult:
        self._verify_plan_is_fresh(plan)
        executable = plan.executable_operations()
        if not executable:
            return ExecutionResult(completed=0, skipped=len(plan.operations))

        loaded_state = self.state_store.load()
        loaded_groups = loaded_state.get("groups", {}) if isinstance(loaded_state, dict) else {}
        previous_groups: Dict[str, Dict[str, object]] = {
            str(global_id): record
            for global_id, record in loaded_groups.items()
            if isinstance(record, dict)
        } if isinstance(loaded_groups, dict) else {}

        for operation in executable:
            source = operation.source_note
            if source and operation.action != OperationAction.DELETE:
                for target in operation.targets:
                    self.adapters[target].preflight_write(source)

        self.state_store.backup()
        completed = 0
        skipped = len(plan.operations) - len(executable)
        errors: List[str] = []
        total = len(executable)
        cancelled = False
        successful_ids: Set[str] = set()
        successful_native_ids: Dict[str, Dict[Endpoint, str]] = {}
        failed_ids: Set[str] = set()
        blocked_ids: Set[str] = {
            operation.global_id
            for operation in plan.operations
            if operation.global_id and not operation.executable
        }

        for index, operation in enumerate(executable, start=1):
            if cancel_event and cancel_event.is_set():
                skipped += total - index + 1
                cancelled = True
                blocked_ids.update(
                    item.global_id
                    for item in executable[index - 1 :]
                    if item.global_id
                )
                break
            if progress:
                progress(index - 1, total, f"正在处理：{operation.title}")
            try:
                operation_native_ids = {
                    endpoint: note.native_id
                    for endpoint, note in operation.versions.items()
                }
                if operation.action == OperationAction.LINK:
                    for endpoint, note in operation.versions.items():
                        if (
                            note.global_id != operation.global_id
                            or bool(note.native.get("metadata_needs_repair"))
                        ):
                            self.adapters[endpoint].set_global_id(note, operation.global_id)
                elif operation.action == OperationAction.DELETE:
                    for target in operation.targets:
                        note = operation.versions.get(target)
                        if note:
                            self.adapters[target].move_to_trash(note)
                else:
                    source = operation.source_note
                    if source is None:
                        raise SyncEngineError("同步操作缺少已选择的来源版本。")
                    # 先给来源写入统一 ID。即使后续某个目标写入失败，已经成功
                    # 的来源/目标仍能在下次预览中恢复为同一组并安全重试。
                    if source.endpoint not in operation.targets and (
                        source.global_id != operation.global_id
                        or bool(source.native.get("metadata_needs_repair"))
                    ):
                        self.adapters[source.endpoint].set_global_id(source, operation.global_id)
                    for target in operation.targets:
                        existing = operation.versions.get(target)
                        folder = operation.target_folders.get(target, source.folder)
                        operation_native_ids[target] = self.adapters[target].upsert_note(
                            source,
                            existing,
                            folder,
                            operation.global_id,
                        )
                completed += 1
                if operation.global_id:
                    successful_ids.add(operation.global_id)
                    successful_native_ids[operation.global_id] = operation_native_ids
            except (AdapterError, SyncEngineError, OSError, ValueError) as exc:
                errors.append(f"{operation.title}：{exc}")
                if operation.global_id:
                    failed_ids.add(operation.global_id)

        if progress:
            message = "同步已取消" if cancelled else "正在保存同步状态……"
            progress(completed, total, message)
        refreshed = self.scan(plan.options.endpoints)
        current_snapshot = self._snapshot_state(refreshed, successful_native_ids)
        final_groups = dict(previous_groups)
        for global_id in successful_ids - failed_ids - blocked_ids:
            merged = self._merge_selected_snapshot(
                final_groups.get(global_id),
                current_snapshot.get(global_id),
                plan.options.endpoints,
            )
            if merged is not None:
                final_groups[global_id] = merged
            else:
                final_groups.pop(global_id, None)

            # 同一路径孤立副本被重新关联后，清除旧状态中指向同一原生笔记
            # 的别名，避免旧同步 ID 在后续扫描中继续干扰分组。
            current_endpoints = self._record_endpoints(current_snapshot.get(global_id))
            for stale_id, stale_record in list(final_groups.items()):
                if stale_id == global_id:
                    continue
                stale_endpoints = dict(self._record_endpoints(stale_record))
                changed = False
                for endpoint in plan.options.endpoints:
                    current_record = current_endpoints.get(endpoint.value, {})
                    stale_endpoint_record = stale_endpoints.get(endpoint.value, {})
                    current_native_id = str(current_record.get("native_id", ""))
                    if (
                        current_native_id
                        and str(stale_endpoint_record.get("native_id", "")) == current_native_id
                    ):
                        stale_endpoints.pop(endpoint.value, None)
                        changed = True
                if not changed:
                    continue
                if stale_endpoints:
                    cleaned_record = dict(stale_record)
                    cleaned_record["endpoints"] = stale_endpoints
                    final_groups[stale_id] = cleaned_record
                else:
                    final_groups.pop(stale_id, None)
        # 第一次同步若只写成了部分目标，仍保存已经落盘的统一 ID，便于
        # 下次准确补写缺失目标；已有组失败时则保留旧基线以继续提示变化。
        for global_id in failed_ids:
            if global_id not in final_groups and global_id in current_snapshot:
                final_groups[global_id] = current_snapshot[global_id]
        self.state_store.save(final_groups)
        if progress:
            progress(completed, total, "同步完成" if not cancelled else "同步已取消")
        return ExecutionResult(completed=completed, skipped=skipped, errors=errors)
