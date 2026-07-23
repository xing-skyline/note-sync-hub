from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import Endpoint, Note, normalize_folder


class AdapterError(RuntimeError):
    pass


class NoteAdapter(ABC):
    endpoint: Endpoint

    @abstractmethod
    def test_connection(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def list_folders(self) -> List[str]:
        raise NotImplementedError

    @abstractmethod
    def list_notes(self) -> List[Note]:
        raise NotImplementedError

    @abstractmethod
    def upsert_note(
        self,
        source: Note,
        existing: Optional[Note],
        folder: str,
        global_id: str,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def set_global_id(self, note: Note, global_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def move_to_trash(self, note: Note) -> None:
        raise NotImplementedError

    def preflight_write(self, source: Note) -> None:
        for asset in source.assets.values():
            asset.load()

    def normalize_target_folder(self, folder: str) -> str:
        return normalize_folder(folder)

    def normalize_target_title(self, title: str) -> str:
        return title
