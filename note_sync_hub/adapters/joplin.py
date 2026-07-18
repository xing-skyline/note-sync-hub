from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from ..attachments import (
    bytes_sha256,
    canonical_asset_uri,
    replace_canonical_asset_uris,
    replace_joplin_resource_links,
)
from ..config import AppConfig
from ..metadata import SyncMetadata, apply_joplin_metadata, extract_joplin_metadata, strip_joplin_metadata
from ..models import Asset, Endpoint, Note, normalize_folder
from .base import AdapterError, NoteAdapter


RESOURCE_ID_RE = re.compile(r":/([a-fA-F0-9]{32})")
LEGACY_TRASH_NOTEBOOK = "_Joplin-Obsidian-Sync Trash"


def sanitize_filename(value: str, max_length: int = 200) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "attachment")
    cleaned = cleaned.rstrip(" .") or "attachment"
    return cleaned[:max_length].rstrip(" .") or "attachment"


class JoplinAdapter(NoteAdapter):
    endpoint = Endpoint.JOPLIN

    def __init__(self, config: AppConfig):
        self.config = config
        self.session = requests.Session()
        self._folders_by_id: Optional[Dict[str, Dict[str, str]]] = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        form_data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> requests.Response:
        query = dict(params or {})
        query["token"] = self.config.joplin_token
        url = f"{self.config.joplin_api_base.rstrip('/')}/{path.lstrip('/')}"
        try:
            response = self.session.request(
                method,
                url,
                params=query,
                json=json_data,
                data=form_data,
                files=files,
                timeout=timeout or self.config.request_timeout,
            )
        except requests.Timeout as exc:
            raise AdapterError("连接 Joplin 超时，请确认 Joplin 和 Web Clipper 服务正在运行。") from exc
        except requests.ConnectionError as exc:
            raise AdapterError("无法连接 Joplin，请确认 Web Clipper 已启用且端口正确。") from exc
        except requests.RequestException as exc:
            raise AdapterError(f"访问 Joplin 失败：{exc}") from exc
        if not 200 <= response.status_code < 300:
            detail = response.text.strip().replace("\n", " ")[:300]
            if response.status_code in {401, 403}:
                raise AdapterError("Joplin 拒绝访问，请检查 Token。")
            raise AdapterError(f"Joplin API 返回 {response.status_code}：{detail}")
        return response

    def _paged(self, path: str, fields: str) -> Iterable[Dict[str, Any]]:
        page = 1
        while True:
            response = self._request(
                "GET",
                path,
                params={"fields": fields, "page": page, "limit": 100},
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise AdapterError("Joplin 返回了无法解析的数据。") from exc
            yield from payload.get("items", [])
            if not payload.get("has_more", False):
                break
            page += 1
            if page > 10000:
                raise AdapterError("Joplin 分页数量异常，已停止扫描。")

    def test_connection(self) -> str:
        self._request("GET", "/folders", params={"limit": 1, "fields": "id"})
        return "Joplin 连接成功"

    def normalize_target_folder(self, folder: str) -> str:
        return normalize_folder(folder) or normalize_folder(self.config.joplin_default_notebook)

    def _load_folders(self, refresh: bool = False) -> Dict[str, Dict[str, str]]:
        if self._folders_by_id is None or refresh:
            self._folders_by_id = {
                item["id"]: {
                    "id": item["id"],
                    "title": item.get("title", ""),
                    "parent_id": item.get("parent_id", "") or "",
                }
                for item in self._paged("/folders", "id,title,parent_id")
            }
        return self._folders_by_id

    def _folder_path(self, folder_id: str) -> str:
        folders = self._load_folders()
        if not folder_id or folder_id not in folders:
            return ""
        parts: List[str] = []
        current = folder_id
        visited = set()
        while current and current in folders and current not in visited:
            visited.add(current)
            item = folders[current]
            parts.insert(0, item["title"])
            current = item.get("parent_id", "")
        return normalize_folder("/".join(parts))

    def list_folders(self) -> List[str]:
        folders = self._load_folders(refresh=True)
        return sorted({"", *(self._folder_path(folder_id) for folder_id in folders)}, key=str.casefold)

    def _note_tags(self, note_id: str) -> Tuple[str, ...]:
        values = [
            item.get("title", "")
            for item in self._paged(f"/notes/{note_id}/tags", "id,title")
            if item.get("title")
        ]
        return tuple(dict.fromkeys(values))

    def _list_note_resources(self, note_id: str) -> List[Dict[str, Any]]:
        return list(
            self._paged(
                f"/notes/{note_id}/resources",
                "id,title,mime,file_extension,filename,updated_time,user_updated_time",
            )
        )

    def _download_resource(self, resource: Dict[str, Any]) -> Tuple[str, bytes, str]:
        resource_id = str(resource.get("id", ""))
        title = str(resource.get("title", "") or resource.get("filename", "") or resource_id)
        extension = str(resource.get("file_extension", "") or "").lstrip(".")
        mime = str(resource.get("mime", "") or "application/octet-stream")
        if not extension:
            extension = (mimetypes.guess_extension(mime) or "").lstrip(".")
        filename = sanitize_filename(title)
        if extension and not filename.casefold().endswith("." + extension.casefold()):
            filename = f"{filename}.{extension}"
        response = self._request(
            "GET",
            f"/resources/{resource_id}/file",
            timeout=max(self.config.request_timeout, 60),
        )
        return filename, response.content, mime

    def list_notes(self) -> List[Note]:
        self._load_folders(refresh=True)
        notes: List[Note] = []
        fields = "id,title,body,parent_id,user_updated_time"
        for item in self._paged("/notes", fields):
            notebook = self._folder_path(item.get("parent_id", ""))
            if notebook == LEGACY_TRASH_NOTEBOOK or notebook.startswith(LEGACY_TRASH_NOTEBOOK + "/"):
                continue
            note_id = str(item["id"])
            raw_body = str(item.get("body", "") or "")
            metadata = extract_joplin_metadata(raw_body)
            resource_ids = {value.casefold() for value in RESOURCE_ID_RE.findall(raw_body)}
            resources = [
                resource
                for resource in self._list_note_resources(note_id)
                if str(resource.get("id", "")).casefold() in resource_ids
            ]
            assets: Dict[str, Asset] = {}
            links: Dict[str, str] = {}
            revision_parts = [str(item.get("user_updated_time", 0) or 0)]
            for resource in resources:
                resource_id = str(resource.get("id", "")).casefold()
                filename, data, mime = self._download_resource(resource)
                digest = bytes_sha256(data)
                assets.setdefault(
                    digest,
                    Asset(
                        digest=digest,
                        filename=filename,
                        size=len(data),
                        source_ref=resource_id,
                        media_type=mime,
                        _data=data,
                    ),
                )
                links[resource_id] = canonical_asset_uri(digest, filename)
                revision_parts.append(
                    f"{resource_id}:{resource.get('updated_time', '')}:{resource.get('user_updated_time', '')}"
                )
            canonical_body = replace_joplin_resource_links(strip_joplin_metadata(raw_body), links)
            revision = hashlib.sha256("|".join(sorted(revision_parts)).encode("utf-8")).hexdigest()
            notes.append(
                Note(
                    endpoint=self.endpoint,
                    native_id=note_id,
                    global_id=metadata.global_id if metadata else "",
                    title=str(item.get("title", "") or "未命名"),
                    folder=notebook,
                    body=canonical_body,
                    tags=self._note_tags(note_id),
                    updated=int(item.get("user_updated_time", 0) or 0),
                    revision=revision,
                    locator="/".join(part for part in (notebook, str(item.get("title", "") or "未命名")) if part),
                    assets=assets,
                    native={"raw_body": raw_body, "parent_id": item.get("parent_id", "")},
                )
            )
        return notes

    def _ensure_notebook(self, notebook_path: str) -> str:
        normalized = normalize_folder(notebook_path) or normalize_folder(self.config.joplin_default_notebook)
        folders = self._load_folders()
        current_parent = ""
        for title in [part for part in normalized.split("/") if part]:
            existing_id = next(
                (
                    folder_id
                    for folder_id, item in folders.items()
                    if item["title"] == title and item.get("parent_id", "") == current_parent
                ),
                "",
            )
            if existing_id:
                current_parent = existing_id
                continue
            response = self._request(
                "POST",
                "/folders",
                json_data={"title": title, "parent_id": current_parent},
            )
            new_id = str(response.json().get("id", ""))
            if not new_id:
                raise AdapterError(f"Joplin 创建笔记本失败：{title}")
            folders[new_id] = {"id": new_id, "title": title, "parent_id": current_parent}
            current_parent = new_id
        return current_parent

    def _upload_resource(self, asset: Asset) -> str:
        data = asset.load()
        props = json.dumps({"title": asset.filename}, ensure_ascii=False)
        response = self._request(
            "POST",
            "/resources",
            form_data={"props": props},
            files={"data": (asset.filename, data, asset.media_type)},
            timeout=max(self.config.request_timeout, 120),
        )
        resource_id = str(response.json().get("id", ""))
        if not resource_id:
            raise AdapterError(f"Joplin 创建 Resource 失败：{asset.filename}")
        return resource_id

    def _render_body(self, source: Note, existing: Optional[Note], global_id: str) -> str:
        targets: Dict[str, str] = {}
        existing_assets = existing.assets if existing else {}
        for digest, asset in source.assets.items():
            existing_asset = existing_assets.get(digest)
            resource_id = existing_asset.source_ref if existing_asset and existing_asset.source_ref else ""
            if not re.fullmatch(r"[a-fA-F0-9]{32}", resource_id):
                resource_id = self._upload_resource(asset)
            targets[digest] = f":/{resource_id}"
        body = replace_canonical_asset_uris(source.body, targets)
        return apply_joplin_metadata(body, SyncMetadata.create(source.endpoint.value, global_id))

    def _sync_tags(self, note_id: str, tags: Iterable[str]) -> None:
        desired = {str(tag).strip() for tag in tags if str(tag).strip()}
        current_items = list(self._paged(f"/notes/{note_id}/tags", "id,title"))
        current = {str(item.get("title", "")): str(item.get("id", "")) for item in current_items}
        all_tags = {str(item.get("title", "")): str(item.get("id", "")) for item in self._paged("/tags", "id,title")}
        for title in desired:
            tag_id = all_tags.get(title, "")
            if not tag_id:
                response = self._request("POST", "/tags", json_data={"title": title})
                tag_id = str(response.json().get("id", ""))
                if tag_id:
                    all_tags[title] = tag_id
            if tag_id and title not in current:
                self._request("POST", f"/tags/{tag_id}/notes", json_data={"id": note_id})
        for title, tag_id in current.items():
            if title not in desired and tag_id:
                self._request("DELETE", f"/tags/{tag_id}/notes/{note_id}")

    def upsert_note(self, source: Note, existing: Optional[Note], folder: str, global_id: str) -> str:
        body = self._render_body(source, existing, global_id)
        parent_id = self._ensure_notebook(folder)
        payload = {"title": source.title, "body": body, "parent_id": parent_id}
        if existing:
            self._request(
                "PUT",
                f"/notes/{existing.native_id}",
                json_data=payload,
                timeout=max(self.config.request_timeout, 60),
            )
            note_id = existing.native_id
        else:
            response = self._request(
                "POST",
                "/notes",
                json_data=payload,
                timeout=max(self.config.request_timeout, 60),
            )
            note_id = str(response.json().get("id", ""))
            if not note_id:
                raise AdapterError(f"Joplin 创建笔记失败：{source.title}")
        self._sync_tags(note_id, source.tags)
        return note_id

    def set_global_id(self, note: Note, global_id: str) -> None:
        raw_body = str(note.native.get("raw_body", "") or note.body)
        body = apply_joplin_metadata(raw_body, SyncMetadata.create(note.endpoint.value, global_id))
        self._request("PUT", f"/notes/{note.native_id}", json_data={"body": body})

    def move_to_trash(self, note: Note) -> None:
        self._request("DELETE", f"/notes/{note.native_id}")
