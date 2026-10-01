"""Execute a reviewed plan and checkpoint only versions actually confirmed written."""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from .adapters.base import AdapterError
from .models import ExecutionResult, OperationAction as Action, TargetResult
from .planning import validate_destinations

if TYPE_CHECKING:
    from .engine import SyncEngine
    from .models import Note, SyncPlan


def note_record(note: Note) -> dict:
    return {
        "native_id": note.native_id, "title": note.title, "folder": note.folder,
        "signature": note.content_signature, "signature_version": 2,
        "revision": note.revision, "updated": note.updated, "locator": note.locator,
    }


def verify_note(adapter, expected: Note) -> None:
    actual = adapter.read_note(expected.native_id)
    if actual is None or actual.snapshot_key != expected.snapshot_key or actual.global_id != expected.global_id:
        raise AdapterError(f"{adapter.endpoint.label} 的笔记在执行期间发生变化，未覆盖：{expected.locator or expected.title}")


def checkpoint_group(groups, global_id, records) -> None:
    """Update confirmed endpoints, removing old aliases for the same native note."""
    for endpoint, record in records.items():
        if record is None:
            continue
        for stale_id, stale_group in list(groups.items()):
            if stale_id == global_id:
                continue
            endpoints = stale_group.get("endpoints", {})
            if endpoints.get(endpoint, {}).get("native_id") == record["native_id"]:
                endpoints.pop(endpoint)
                if not endpoints:
                    groups.pop(stale_id)
    endpoints = groups.setdefault(global_id, {"endpoints": {}})["endpoints"]
    for endpoint, record in records.items():
        if record is None:
            endpoints.pop(endpoint, None)
        else:
            endpoints[endpoint] = record
    if not endpoints:
        groups.pop(global_id, None)


def execute_plan(engine: SyncEngine, plan: SyncPlan, *, cancel_event=None, progress=None) -> ExecutionResult:
    if cancel_event and cancel_event.is_set():
        return ExecutionResult(0, len(plan.operations), cancelled=True)
    current = engine._verify_plan_is_fresh(plan)
    validate_destinations(plan, current, engine.adapters)
    blocked = {op.global_id for op in plan.operations if op.conflict_kind and op.conflict_kind.blocks_write}
    executable = [op for op in plan.executable_operations() if op.global_id not in blocked]
    result = ExecutionResult(0, len(plan.operations) - len(executable))
    if not executable:
        return result
    groups = deepcopy(engine.state_store.load()["groups"])
    for operation in executable:
        if operation.source_note and operation.action != Action.DELETE:
            for target in operation.targets:
                engine.adapters[target].preflight_write(operation.source_note)
    engine.state_store.backup()

    for index, operation in enumerate(executable):
        if cancel_event and cancel_event.is_set():
            result.skipped += len(executable) - index
            result.cancelled = True
            break
        if progress:
            progress(index, len(executable), f"正在处理：{operation.title}")
        # Cancellation applies between notes. Finish and checkpoint this note first.
        events = {ep: adapter.cancel_event for ep, adapter in engine.adapters.items()}
        for adapter in engine.adapters.values():
            adapter.cancel_event = None
        records = {}
        source_record = None
        source = operation.source_note
        successes = 0
        targets = tuple(operation.versions) if operation.action == Action.LINK else operation.targets
        try:
            for endpoint, version in operation.versions.items():
                verify_note(engine.adapters[endpoint], version)
            if source and source.endpoint not in targets:
                original = operation.versions[source.endpoint]
                if original.global_id != operation.global_id or original.native.get("metadata_needs_repair"):
                    engine.adapters[source.endpoint].set_global_id(original, operation.global_id)
                # Retain the confirmed source version, never a later rescan of it.
                source_record = note_record(source)
            for target in targets:
                adapter = engine.adapters[target]
                existing = operation.versions.get(target)
                try:
                    if existing:
                        verify_note(adapter, existing)
                    if operation.action == Action.DELETE:
                        adapter.move_to_trash(existing)
                        if adapter.read_note(existing.native_id) is not None:
                            raise AdapterError("删除后的副本仍存在，未更新同步状态。")
                        records[target.value] = None
                    elif operation.action == Action.LINK:
                        if existing.global_id != operation.global_id or existing.native.get("metadata_needs_repair"):
                            adapter.set_global_id(existing, operation.global_id)
                        actual = adapter.read_note(existing.native_id)
                        if actual is None or actual.global_id != operation.global_id or (
                            actual.content_signature != existing.content_signature
                            or actual.title != existing.title or actual.folder != existing.folder
                        ):
                            raise AdapterError("建立关联期间内容变化，未更新同步状态。")
                        records[target.value] = note_record(existing)
                    else:
                        folder = operation.target_folders.get(target, source.folder)
                        native_id = adapter.upsert_note(source, existing, folder, operation.global_id)
                        actual = adapter.read_note(native_id)
                        if actual is None or actual.global_id != operation.global_id or not adapter.matches_written(actual, source, folder):
                            raise AdapterError("写入后的内容与确认版本不符，保留旧基线；请重新预览。")
                        records[target.value] = note_record(actual)
                    successes += 1
                    result.targets.append(TargetResult(operation.global_id, operation.title, target, operation.action, True))
                except (AdapterError, OSError, ValueError) as exc:
                    message = f"{operation.title} → {target.label}：{exc}"
                    result.errors.append(message)
                    result.targets.append(TargetResult(operation.global_id, operation.title, target, operation.action, False, str(exc)))
            if successes == len(targets):
                result.completed += 1
                # Endpoints not being written retain their preview versions as baseline.
                for endpoint, version in operation.versions.items():
                    records.setdefault(endpoint.value, note_record(version))
                if operation.action == Action.DELETE:
                    for endpoint in plan.options.endpoints:
                        if endpoint not in operation.versions:
                            records[endpoint.value] = None
                if source_record:
                    records[source.endpoint.value] = source_record
            elif source_record and source.endpoint.value not in groups.get(operation.global_id, {}).get("endpoints", {}):
                # First-run partial writes still need a source identity for safe retry.
                records[source.endpoint.value] = source_record
        except (AdapterError, OSError, ValueError) as exc:
            result.errors.append(f"{operation.title}：{exc}")
        finally:
            for endpoint, adapter in engine.adapters.items():
                adapter.cancel_event = events[endpoint]
            if records:
                checkpoint_group(groups, operation.global_id, records)
                # No final full-library scan: an interruption cannot consume unrelated edits.
                engine.state_store.save(groups)
    if progress:
        progress(result.completed, len(executable), "同步已取消" if result.cancelled else "同步完成")
    return result
