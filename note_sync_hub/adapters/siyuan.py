from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests

from ..attachments import (
    attachment_references,
    bytes_sha256,
    canonical_asset_uri,
    normalized_local_target,
    replace_canonical_asset_uris,
    replace_reference_targets,
)
from ..config import AppConfig
from ..models import Asset, Endpoint, Note, normalize_folder
from .base import AdapterError, NoteAdapter


GLOBAL_ID_ATTR = "custom-notesynchub-id"
TAGS_ATTR = "custom-notesynchub-tags"
CONTAINER_ATTR = "custom-notesynchub-container"


def _safe_document_title(value: str) -> str:
    title = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value or "未命名").strip()
    return title[:128] or "未命名"


class SiYuanAdapter(NoteAdapter):
    endpoint = Endpoint.SIYUAN

    def __init__(self, config: AppConfig):
        self.config = config
        self.session = requests.Session()
        self._notebooks: Optional[Dict[str, str]] = None

    def _request(
        self,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        binary: bool = False,
    ) -> Any:
        url = f"{self.config.siyuan_api_base.rstrip('/')}/{path.lstrip('/')}"
        headers = {"Authorization": f"Token {self.config.siyuan_token}"}
        try:
            response = self.session.post(
                url,
                json=payload or {},
                headers=headers,
                timeout=max(self.config.request_timeout, 60) if binary else self.config.request_timeout,
            )
        except requests.Timeout as exc:
            raise AdapterError("连接思源笔记超时，请确认思源正在运行。") from exc
        except requests.ConnectionError as exc:
            raise AdapterError("无法连接思源笔记，请确认思源已启动且 API 地址正确。") from exc
        except requests.RequestException as exc:
            raise AdapterError(f"访问思源笔记失败：{exc}") from exc
        if not 200 <= response.status_code < 300:
            detail = response.text.strip().replace("\n", " ")[:300]
            if response.status_code in {401, 403}:
                raise AdapterError("思源笔记拒绝访问，请检查 API Token。")
            raise AdapterError(f"思源 API 返回 {response.status_code}：{detail}")
        if binary:
            content_type = response.headers.get("Content-Type", "").casefold()
            if "application/json" not in content_type:
                return response.content
        try:
            result = response.json()
        except ValueError as exc:
            raise AdapterError("思源笔记返回了无法解析的数据。") from exc
        if not isinstance(result, dict):
            raise AdapterError("思源笔记返回了格式异常的数据。")
        if int(result.get("code", 0) or 0) != 0:
            message = str(result.get("msg", "未知错误"))
            if "token" in message.casefold() or "auth" in message.casefold():
                raise AdapterError("思源笔记拒绝访问，请检查 API Token。")
            raise AdapterError(f"思源 API 错误：{message}")
        if binary:
            raise AdapterError(f"思源附件读取失败：{result.get('msg') or '文件不存在'}")
        return result.get("data")

    def test_connection(self) -> str:
        version = self._request("/api/system/version")
        return f"思源笔记连接成功（{version or '版本未知'}）"

    def normalize_target_folder(self, folder: str) -> str:
        notebook, parents = self._split_folder(folder)
        return normalize_folder("/".join([notebook, *(_safe_document_title(part) for part in parents)]))

    def _load_notebooks(self, refresh: bool = False) -> Dict[str, str]:
        if self._notebooks is None or refresh:
            data = self._request("/api/notebook/lsNotebooks") or {}
            items = data.get("notebooks", []) if isinstance(data, dict) else []
            self._notebooks = {
                str(item.get("name", "")): str(item.get("id", ""))
                for item in items
                if item.get("name") and item.get("id") and not item.get("closed", False)
            }
        return self._notebooks

    def _notebook_name(self, notebook_id: str) -> str:
        for name, identifier in self._load_notebooks().items():
            if identifier == notebook_id:
                return name
        return notebook_id

    def _document_rows(self) -> List[Dict[str, Any]]:
        statement = (
            "SELECT id, box, hpath, content, updated, ial, path "
            "FROM blocks WHERE type = 'd' AND box != '' ORDER BY hpath"
        )
        rows = self._request("/api/query/sql", {"stmt": statement}) or []
        return [row for row in rows if isinstance(row, dict)]

    def list_folders(self) -> List[str]:
        folders = set(self._load_notebooks())
        for row in self._document_rows():
            notebook = self._notebook_name(str(row.get("box", "")))
            hpath = normalize_folder(str(row.get("hpath", "")))
            if notebook:
                folders.add(normalize_folder("/".join(part for part in (notebook, hpath) if part)))
        return sorted(folders, key=str.casefold)

    def _attrs(self, block_id: str) -> Dict[str, str]:
        data = self._request("/api/attr/getBlockAttrs", {"id": block_id}) or {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    def _export_markdown(self, block_id: str) -> str:
        data = self._request("/api/export/exportMdContent", {"id": block_id}) or {}
        return str(data.get("content", "")) if isinstance(data, dict) else ""

    @staticmethod
    def _asset_data_path(target: str) -> Optional[str]:
        value = unquote((target or "").strip()).replace("\\", "/")
        parsed = urlparse(value)
        if parsed.scheme or value.startswith("#"):
            return None
        value = value.split("#", 1)[0].split("?", 1)[0]
        marker = "assets/"
        index = value.casefold().find(marker)
        if index < 0:
            return None
        relative = value[index:].lstrip("/")
        return "/data/" + relative

    def _read_asset(self, data_path: str) -> bytes:
        data = self._request("/api/file/getFile", {"path": data_path}, binary=True)
        if not isinstance(data, bytes):
            raise AdapterError(f"思源附件读取失败：{data_path}")
        return data

    @staticmethod
    def _parse_tags(attrs: Dict[str, str]) -> Tuple[str, ...]:
        raw = attrs.get(TAGS_ATTR, "")
        if not raw:
            return ()
        try:
            value = json.loads(raw)
        except ValueError:
            value = [part.strip() for part in raw.split(",")]
        if not isinstance(value, list):
            return ()
        return tuple(str(tag).strip().lstrip("#") for tag in value if str(tag).strip().lstrip("#"))

    def list_notes(self) -> List[Note]:
        notes: List[Note] = []
        for row in self._document_rows():
            block_id = str(row.get("id", ""))
            if not block_id:
                continue
            attrs = self._attrs(block_id)
            body = self._export_markdown(block_id)
            if attrs.get(CONTAINER_ATTR) == "1" and not body.strip():
                continue

            notebook = self._notebook_name(str(row.get("box", "")))
            hpath = normalize_folder(str(row.get("hpath", "")))
            path_parts = hpath.split("/") if hpath else []
            title = path_parts[-1] if path_parts else str(row.get("content", "") or "未命名")
            parent = "/".join(path_parts[:-1])
            folder = normalize_folder("/".join(part for part in (notebook, parent) if part))

            assets: Dict[str, Asset] = {}
            replacements = []
            revision_parts = [str(row.get("updated", "")), body]
            for reference in attachment_references(body):
                if normalized_local_target(reference) is None:
                    continue
                data_path = self._asset_data_path(reference.target)
                if not data_path:
                    continue
                data = self._read_asset(data_path)
                digest = bytes_sha256(data)
                filename = Path(unquote(data_path)).name or "attachment.bin"
                assets.setdefault(
                    digest,
                    Asset(
                        digest=digest,
                        filename=filename,
                        size=len(data),
                        source_ref=data_path,
                        _data=data,
                    ),
                )
                replacements.append((reference, canonical_asset_uri(digest, filename), filename))
                revision_parts.append(f"{data_path}:{digest}")
            canonical_body = replace_reference_targets(body, replacements)
            revision = bytes_sha256("|".join(revision_parts).encode("utf-8"))
            notes.append(
                Note(
                    endpoint=self.endpoint,
                    native_id=block_id,
                    global_id=attrs.get(GLOBAL_ID_ATTR, ""),
                    title=title,
                    folder=folder,
                    body=canonical_body,
                    tags=self._parse_tags(attrs),
                    updated=int(str(row.get("updated", "0") or "0")),
                    revision=revision,
                    locator=normalize_folder("/".join(part for part in (notebook, hpath) if part)),
                    assets=assets,
                    native={
                        "notebook_id": str(row.get("box", "")),
                        "hpath": hpath,
                        "attrs": attrs,
                    },
                )
            )
        return notes

    def _ensure_notebook(self, name: str) -> str:
        notebooks = self._load_notebooks()
        if name in notebooks:
            return notebooks[name]
        data = self._request("/api/notebook/createNotebook", {"name": name}) or {}
        notebook = data.get("notebook", data) if isinstance(data, dict) else {}
        notebook_id = str(notebook.get("id", "")) if isinstance(notebook, dict) else ""
        if not notebook_id:
            self._load_notebooks(refresh=True)
            notebook_id = self._load_notebooks().get(name, "")
        if not notebook_id:
            raise AdapterError(f"思源笔记本创建失败：{name}")
        self._load_notebooks(refresh=True)
        return notebook_id

    def _split_folder(self, folder: str) -> Tuple[str, List[str]]:
        parts = normalize_folder(folder).split("/") if normalize_folder(folder) else []
        notebook_name = parts[0] if parts else (self.config.siyuan_default_notebook or "Note Sync Hub")
        return notebook_name, parts[1:]

    def _ids_by_hpath(self, notebook_id: str, hpath: str) -> List[str]:
        data = self._request(
            "/api/filetree/getIDsByHPath",
            {"path": "/" + normalize_folder(hpath), "notebook": notebook_id},
        ) or []
        return [str(value) for value in data if value]

    def _create_document(self, notebook_id: str, hpath: str, markdown: str) -> str:
        data = self._request(
            "/api/filetree/createDocWithMd",
            {"notebook": notebook_id, "path": "/" + normalize_folder(hpath), "markdown": markdown},
        )
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            return str(data.get("id", ""))
        return ""

    def _set_attrs(self, block_id: str, attrs: Dict[str, str]) -> None:
        self._request("/api/attr/setBlockAttrs", {"id": block_id, "attrs": attrs})

    def _ensure_parent(self, folder: str) -> Tuple[str, str]:
        notebook_name, parents = self._split_folder(folder)
        notebook_id = self._ensure_notebook(notebook_name)
        parent_id = notebook_id
        current: List[str] = []
        for part in parents:
            current.append(_safe_document_title(part))
            hpath = "/".join(current)
            matches = self._ids_by_hpath(notebook_id, hpath)
            if matches:
                parent_id = matches[-1]
                continue
            parent_id = self._create_document(notebook_id, hpath, "")
            if not parent_id:
                raise AdapterError(f"思源父文档创建失败：{notebook_name}/{hpath}")
            self._set_attrs(parent_id, {CONTAINER_ATTR: "1"})
        return notebook_id, parent_id

    def _upload_asset(self, asset: Asset) -> str:
        url = f"{self.config.siyuan_api_base.rstrip('/')}/api/asset/upload"
        headers = {"Authorization": f"Token {self.config.siyuan_token}"}
        filename = Path(asset.filename or "attachment.bin").name
        media_type = asset.media_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            response = self.session.post(
                url,
                data={"assetsDirPath": "/assets/"},
                files=[("file[]", (filename, asset.load(), media_type))],
                headers=headers,
                timeout=max(self.config.request_timeout, 120),
            )
        except requests.RequestException as exc:
            raise AdapterError(f"上传思源附件失败：{filename}（{exc}）") from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise AdapterError(f"上传思源附件失败：{filename}") from exc
        if not 200 <= response.status_code < 300 or int(result.get("code", 0) or 0) != 0:
            raise AdapterError(f"上传思源附件失败：{filename}（{result.get('msg', response.status_code)}）")
        data = result.get("data") or {}
        success = data.get("succMap", {}) if isinstance(data, dict) else {}
        path = str(success.get(filename, ""))
        if not path and success:
            path = str(next(iter(success.values())))
        if not path:
            errors = data.get("errFiles", []) if isinstance(data, dict) else []
            raise AdapterError(f"上传思源附件失败：{filename}（{errors or '未返回文件路径'}）")
        return path.lstrip("/")

    def _render_body(self, source: Note, existing: Optional[Note]) -> str:
        targets: Dict[str, str] = {}
        existing_assets = existing.assets if existing else {}
        for digest, asset in source.assets.items():
            previous = existing_assets.get(digest)
            if previous and previous.source_ref.startswith("/data/assets/"):
                targets[digest] = previous.source_ref[len("/data/") :]
            else:
                targets[digest] = self._upload_asset(asset)
        return replace_canonical_asset_uris(source.body, targets)

    def upsert_note(self, source: Note, existing: Optional[Note], folder: str, global_id: str) -> str:
        notebook_id, parent_id = self._ensure_parent(folder)
        notebook_name, parents = self._split_folder(folder)
        title = _safe_document_title(source.title)
        hpath = "/".join([*(_safe_document_title(part) for part in parents), title])
        body = self._render_body(source, existing)

        if existing:
            block_id = existing.native_id
            existing_notebook = str(existing.native.get("notebook_id", ""))
            existing_folder = normalize_folder(existing.folder)
            if existing_notebook != notebook_id or existing_folder != normalize_folder(folder):
                self._request("/api/filetree/moveDocsByID", {"fromIDs": [block_id], "toID": parent_id})
            if existing.title != title:
                self._request("/api/filetree/renameDocByID", {"id": block_id, "title": title})
            self._request("/api/block/updateBlock", {"dataType": "markdown", "data": body, "id": block_id})
        else:
            matches = self._ids_by_hpath(notebook_id, hpath)
            if matches:
                candidate = matches[-1]
                if self._attrs(candidate).get(CONTAINER_ATTR) != "1":
                    raise AdapterError(f"思源目标路径已有未关联文档：{notebook_name}/{hpath}")
                block_id = candidate
                self._request("/api/block/updateBlock", {"dataType": "markdown", "data": body, "id": block_id})
            else:
                block_id = self._create_document(notebook_id, hpath, body)
            if not block_id:
                raise AdapterError(f"思源文档创建失败：{notebook_name}/{hpath}")

        self._set_attrs(
            block_id,
            {
                GLOBAL_ID_ATTR: global_id,
                TAGS_ATTR: json.dumps(list(source.tags), ensure_ascii=False),
                CONTAINER_ATTR: "",
            },
        )
        return block_id

    def set_global_id(self, note: Note, global_id: str) -> None:
        self._set_attrs(note.native_id, {GLOBAL_ID_ATTR: global_id})

    def move_to_trash(self, note: Note) -> None:
        raise AdapterError("为防止误删，Note Sync Hub 第一版不会自动删除思源文档。")
