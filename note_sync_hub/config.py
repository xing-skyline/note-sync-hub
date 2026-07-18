from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .models import Endpoint


APP_DIR_NAME = "NoteSyncHub"


def app_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path.home() / ".config"
    return base / APP_DIR_NAME


def default_config_path() -> Path:
    return app_data_dir() / "config.json"


@dataclass
class AppConfig:
    joplin_api_base: str = "http://127.0.0.1:41184"
    joplin_token: str = ""
    obsidian_vault_path: str = ""
    siyuan_api_base: str = "http://127.0.0.1:6806"
    siyuan_token: str = ""
    request_timeout: int = 30
    obsidian_attachments_folder: str = "attachments"
    joplin_default_notebook: str = "Note Sync Hub"
    siyuan_default_notebook: str = "Note Sync Hub"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        allowed = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
        config = cls(**allowed)
        config.joplin_api_base = config.joplin_api_base.rstrip("/")
        config.siyuan_api_base = config.siyuan_api_base.rstrip("/")
        return config

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def validate(self, endpoints: Iterable[Endpoint]) -> None:
        selected = set(endpoints)
        if len(selected) < 1:
            raise ValueError("请至少选择一个笔记端。")
        if self.request_timeout < 1:
            raise ValueError("网络超时必须大于 0 秒。")
        if Endpoint.JOPLIN in selected:
            if not self.joplin_api_base.strip():
                raise ValueError("请填写 Joplin API 地址。")
            if not self.joplin_token.strip():
                raise ValueError("请填写 Joplin Token。")
        if Endpoint.OBSIDIAN in selected:
            if not self.obsidian_vault_path.strip():
                raise ValueError("请选择 Obsidian Vault。")
            vault = Path(self.obsidian_vault_path).expanduser()
            if not vault.is_dir():
                raise ValueError(f"Obsidian Vault 不存在或不是文件夹：{vault}")
        if Endpoint.SIYUAN in selected:
            if not self.siyuan_api_base.strip():
                raise ValueError("请填写思源 API 地址。")
            if not self.siyuan_token.strip():
                raise ValueError("请填写思源 API Token。")

    def state_path(self) -> Path:
        vault_identity = (
            str(Path(self.obsidian_vault_path).expanduser().resolve()).casefold()
            if self.obsidian_vault_path.strip()
            else "<no-obsidian-vault>"
        )
        identity = "|".join(
            [
                self.joplin_api_base.casefold(),
                vault_identity,
                self.siyuan_api_base.casefold(),
            ]
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        return app_data_dir() / "state" / f"{digest}.json"


def load_config(path: Optional[Path] = None) -> AppConfig:
    config_path = Path(path) if path else default_config_path()
    if not config_path.is_file():
        return AppConfig()
    with config_path.open("r", encoding="utf-8") as handle:
        return AppConfig.from_dict(json.load(handle))


def save_config(config: AppConfig, path: Optional[Path] = None) -> Path:
    config_path = Path(path) if path else default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(config.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(str(temporary), str(config_path))
    return config_path
