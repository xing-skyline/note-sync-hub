from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


STATE_VERSION = 1


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def empty() -> Dict[str, Any]:
        return {"version": STATE_VERSION, "groups": {}}

    def load(self) -> Dict[str, Any]:
        if not self.path.is_file():
            return self.empty()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return self.empty()
        if not isinstance(payload, dict) or not isinstance(payload.get("groups"), dict):
            return self.empty()
        return payload

    def save(self, groups: Dict[str, Dict[str, Any]]) -> None:
        payload = {
            "version": STATE_VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "groups": groups,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temporary), str(self.path))

    def backup(self) -> Optional[Path]:
        if not self.path.is_file():
            return None
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = self.path.with_name(f"{self.path.stem}-{suffix}.bak.json")
        destination.write_bytes(self.path.read_bytes())
        return destination
