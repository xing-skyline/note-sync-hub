from __future__ import annotations

import json
import os
import hashlib
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


STATE_VERSION = 1
_held_locks = threading.local()


def state_fingerprint(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=path.name + ".", suffix=".tmp",
                                         dir=str(path.parent), delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.backup_path = self.path.with_suffix(self.path.suffix + ".bak")

    @contextmanager
    def lock(self):
        """Hold across scanning, note writes and checkpoints; released on process exit."""
        held = getattr(_held_locks, "paths", None)
        if held is None:
            held = _held_locks.paths = set()
        if self.path in held:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Never unlink this file: waiters must continue to lock the same inode.
        with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ValueError("同步状态正在被其他实例使用，请等待其结束后重新预览。") from exc
            held.add(self.path)
            try:
                yield
            finally:
                held.remove(self.path)
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def empty() -> Dict[str, Any]:
        return {"version": STATE_VERSION, "groups": {}}

    def _decode(self, raw: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ValueError(f"同步状态无法读取，已停止同步。请保留该文件并检查备份：{self.path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("groups"), dict):
            raise ValueError(f"同步状态格式无效，已停止同步：{self.path}")
        if type(payload.get("version")) is not int or payload["version"] != STATE_VERSION:
            raise ValueError(f"不支持的同步状态版本，已停止同步：{self.path}")
        for record in payload["groups"].values():
            if not isinstance(record, dict) or not isinstance(record.get("endpoints"), dict):
                raise ValueError(f"同步状态中的笔记关联无效，已停止同步：{self.path}")
            if any(not isinstance(value, dict) for value in record["endpoints"].values()):
                raise ValueError(f"同步状态中的端点记录无效，已停止同步：{self.path}")
        return payload

    def _read(self) -> Optional[bytes]:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            if self.backup_path.exists() or any(self.path.parent.glob(f"{self.path.stem}-*.bak.json")):
                raise ValueError(f"同步状态文件缺失但备份仍在，不能按首次同步处理：{self.path}。请核对并恢复备份。")
            return None
        except OSError as exc:
            raise ValueError(f"无法读取同步状态，已停止：{self.path}。请检查路径和访问权限。") from exc

    def load(self) -> Dict[str, Any]:
        with self.lock():
            raw = self._read()
            return self._decode(raw) if raw is not None else self.empty()

    def save(self, groups: Dict[str, Dict[str, Any]]) -> None:
        payload = {
            "version": STATE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
        }
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self._decode(data)
        with self.lock():
            previous = self._read()
            if previous is not None:
                self._decode(previous)
            # Even the first save leaves evidence if the main state later disappears.
            _atomic_write(self.backup_path, previous if previous is not None else data)
            _atomic_write(self.path, data)

    def backup(self) -> Optional[Path]:
        with self.lock():
            raw = self._read()
            if raw is None:
                return None
            self._decode(raw)
            suffix = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + uuid.uuid4().hex[:8]
            destination = self.path.with_name(f"{self.path.stem}-{suffix}.bak.json")
            _atomic_write(destination, raw)
            return destination
