"""Persist named sync options, scoped to a connection set and without credentials."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .config import app_data_dir
from .models import ConflictPolicy, Endpoint, SyncMode, SyncOptions, TargetMode


def decode_options(data: dict) -> SyncOptions:
    try:
        options = SyncOptions(
            mode=SyncMode(data["mode"]), endpoints=tuple(Endpoint(value) for value in data["endpoints"]),
            source=Endpoint(data["source"]) if data.get("source") else None,
            primary=Endpoint(data["primary"]) if data.get("primary") else None,
            scope_all=data.get("scope_all", True),
            selected_folders={Endpoint(key): tuple(value) for key, value in data.get("selected_folders", {}).items()},
            include_subfolders=data.get("include_subfolders", True),
            target_mode=TargetMode(data.get("target_mode", "preserve")),
            target_folders={Endpoint(key): value for key, value in data.get("target_folders", {}).items()},
            propagate_deletions=data.get("propagate_deletions", False),
            conflict_policy=ConflictPolicy(data.get("conflict_policy", "manual")),
        )
        if any(type(value) is not bool for value in (options.scope_all, options.include_subfolders, options.propagate_deletions)):
            raise ValueError("同步开关必须为布尔值")
        options.validate()
        return options
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError(f"保存的同步方案无效：{exc}") from exc


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or app_data_dir() / "profiles.json"

    def _load(self):
        if not self.path.exists():
            return {"version": 1, "profiles": {}, "last": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("profiles"), dict) or not isinstance(data.get("last"), dict):
                raise ValueError("格式或版本不受支持")
            return data
        except (OSError, ValueError, AttributeError) as exc:
            raise ValueError(f"无法读取同步方案，请检查文件：{self.path}") from exc

    def names(self, identity: str) -> list[str]:
        return sorted(self._load()["profiles"].get(identity, {}), key=str.casefold)

    def last_name(self, identity: str) -> str:
        return self._load()["last"].get(identity, "")

    def get(self, name: str, identity: str) -> SyncOptions:
        data = self._load()["profiles"].get(identity, {}).get(name)
        if not isinstance(data, dict):
            raise ValueError("当前连接组合没有此同步方案，请检查连接设置。")
        return decode_options(data)

    def save(self, name: str, options: SyncOptions, identity: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("请填写同步方案名称。")
        options.validate()
        data = self._load()
        data["profiles"].setdefault(identity, {})[name] = asdict(options)
        data["last"][identity] = name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
