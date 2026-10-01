from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
import threading
from typing import List, Optional

from ..models import Endpoint, Note, normalize_folder
from ..attachments import CANONICAL_ASSET_RE


class AdapterError(RuntimeError):
    pass


class ScanCancelled(AdapterError):
    pass


class NoteNotFound(AdapterError):
    pass


class NoteAdapter(ABC):
    endpoint: Endpoint
    cancel_event: Optional[threading.Event] = None

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise ScanCancelled("扫描已取消，未继续执行同步。")

    def read_note(self, native_id: str) -> Optional[Note]:
        return next((note for note in self.list_notes() if note.native_id == native_id), None)

    def matches_written(self, actual: Note, source: Note, folder: str) -> bool:
        expected = replace(source, title=self.normalize_target_title(source.title), folder=self.normalize_target_folder(folder))
        # Reusing an existing attachment may preserve its original filename.
        def comparable(note):
            body = CANONICAL_ASSET_RE.sub(lambda match: f"notesync-asset://{match.group('digest').lower()}/asset", note.body)
            return replace(note, body=body).content_signature
        return (
            comparable(actual) == comparable(expected)
            and actual.title == expected.title and actual.folder == expected.folder
        )

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

    def target_locator(self, folder: str, title: str) -> str:
        return "/".join(part for part in (self.normalize_target_folder(folder), self.normalize_target_title(title)) if part)
