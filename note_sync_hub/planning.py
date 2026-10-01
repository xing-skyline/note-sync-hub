"""Shared plan validation and conflict resolution, independent of the desktop UI."""
from __future__ import annotations

from dataclasses import replace

from .models import ConflictKind, OperationAction, SyncMode


def validate_destinations(plan, notes, adapters):
    blocked_ids = {op.global_id: op.reason for op in plan.operations
                   if op.conflict_kind == ConflictKind.IDENTITY and op.global_id}
    owners = {}
    for operation in plan.operations:
        if operation.global_id in blocked_ids:
            operation.action = OperationAction.CONFLICT
            operation.conflict_kind = ConflictKind.IDENTITY
            operation.reason = blocked_ids[operation.global_id]
        source = operation.source_note
        if not source or operation.action in {OperationAction.DELETE, OperationAction.LINK}:
            continue
        for target in operation.targets:
            adapter = adapters[target]
            folder = operation.target_folders.get(target, source.folder)
            title = adapter.normalize_target_title(source.title)
            operation.target_paths[target] = adapter.target_locator(folder, title)
            path = "/".join(part for part in (folder, title) if part).casefold()
            previous = owners.get((target, path))
            existing = operation.versions.get(target)
            occupied = [note for note in notes.get(target, []) if note.path_key == path
                        and (existing is None or note.native_id != existing.native_id)]
            if occupied or previous is not None:
                for blocked in (operation, previous):
                    if blocked is not None:
                        blocked.action = OperationAction.CONFLICT
                        blocked.conflict_kind = ConflictKind.PATH
                        blocked.reason = f"{target.label} 目标路径冲突：{folder}/{title}。请先调整标题或目录，再重新预览。"
            owners[(target, path)] = operation


def resolve_conflict(plan, operation, merged):
    if not any(item is operation for item in plan.operations) or not operation.can_resolve:
        raise ValueError("该问题不能通过覆盖正文解决，请先修复关联、路径或附件。")
    if merged.endpoint not in operation.versions:
        raise ValueError("合并内容的来源端不在原冲突中。")
    if any(note.native.get("attachment_issues") for note in operation.versions.values()):
        raise ValueError("请先修复附件，再解决内容冲突。")
    targets = plan.options.endpoints
    if plan.options.mode == SyncMode.ONE_WAY and operation.conflict_kind != ConflictKind.DELETE_MODIFY:
        targets = plan.options.targets
    operation.resolved_note = replace(merged, assets=dict(merged.assets))
    operation.source = merged.endpoint
    operation.title = merged.title
    operation.targets = tuple(targets)
    operation.target_folders = {
        endpoint: operation.versions[endpoint].folder if endpoint in operation.versions else merged.folder
        for endpoint in targets
    }
    operation.action = OperationAction.UPDATE
    operation.reason = "已确认合并结果；执行时写入列出的目标端。"
