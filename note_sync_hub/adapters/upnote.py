"""Local UpNote adapter, verified against the Windows 9.22 database schema.

Reads consistent SQLite snapshots; writes only while UpNote is fully exited.
The app owns network synchronization. No MCP, private cloud API or UI automation.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
import csv
from dataclasses import replace
from datetime import datetime
import hashlib
from html import escape
import io
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from urllib.parse import unquote, urlparse
import uuid

from bs4 import BeautifulSoup, Comment, NavigableString
from markdown import Markdown
from markdownify import markdownify

from ..attachments import canonical_asset_digest, canonical_asset_uri
from ..config import AppConfig
from ..models import Asset, Endpoint, Note, normalize_folder
from .base import AdapterError, NoteAdapter


SCHEMA = {
    "notes": {
        "id",
        "title",
        "html",
        "text",
        "summary",
        "revision",
        "synced",
        "trashed",
        "deleted",
        "space",
        "tagLinks",
        "fileIds",
        "firstImage",
        "filesCount",
        "updatedAt",
        "createdAt",
        "isTemplate",
        "noteLinks",
        "notebookLinks",
        "hasTodo",
        "bookmarked",
        "pinned",
        "highlighted",
    },
    "notebooks": {
        "id",
        "title",
        "parent",
        "space",
        "locked",
        "deleted",
        "revision",
        "synced",
        "notes",
        "childNotebooks",
        "inactive",
        "createdAt",
        "updatedAt",
        "activatedAt",
    },
    "lists": {"id", "content", "space", "deleted", "revision", "synced", "syncedAt"},
    "files": {
        "id",
        "name",
        "revision",
        "deleted",
        "synced",
        "downloadURL",
        "createdAt",
        "updatedAt",
    },
    "tags": {
        "id",
        "title",
        "space",
        "deleted",
        "synced",
        "revision",
        "notes",
        "inactive",
        "createdAt",
        "updatedAt",
        "activatedAt",
    },
}
FILE_NAME = re.compile(r"[a-fA-F0-9-]+\.[a-zA-Z0-9]+\Z")


def is_upnote_running() -> bool:
    if sys.platform != "win32":
        raise AdapterError("UpNote 写入目前仅在 Windows 上验证。")
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
        if result.returncode:
            raise AdapterError("无法确认 UpNote 是否运行，已停止写入。")
        return any(
            row and row[0].casefold() == "upnote.exe"
            for row in csv.reader(io.StringIO(result.stdout))
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError("无法确认 UpNote 是否运行，已停止写入。") from exc


def _uuid7() -> str:
    raw = bytearray(int(time.time() * 1000).to_bytes(6, "big") + os.urandom(10))
    raw[6] = (raw[6] & 15) | 112
    raw[8] = (raw[8] & 63) | 128
    return str(uuid.UUID(bytes=bytes(raw)))


def _array(value) -> list[str]:
    try:
        data = json.loads(value or "[]")
        if not isinstance(data, list) or not all(
            isinstance(item, str) for item in data
        ):
            raise ValueError("invalid list")
        return list(dict.fromkeys(data))
    except (TypeError, ValueError) as exc:
        raise AdapterError("UpNote 分组或附件索引无效，已停止扫描。") from exc


def _md(html: str) -> str:
    def language(element):
        code = element.find("code")
        return next(
            (
                item[9:]
                for item in (code.get("class", []) if code else [])
                if item.startswith("language-")
            ),
            "",
        )

    return markdownify(
        html, heading_style="ATX", code_language_callback=language
    ).strip()


def _html(body: str) -> str:
    parser = Markdown(extensions=["extra", "sane_lists", "nl2br"])
    # Obsidian/Joplin hashtags without a space are text, not ATX headings.
    parser.parser.blockprocessors["hashheader"].RE = re.compile(
        r"(^|\n)(?P<level>#{1,6})(?=[ \t\n]|$)[ \t]*(?P<header>.*?)#*(\n|$)"
    )
    return parser.convert(body)


def _rich(soup) -> bool:
    if any(isinstance(node, Comment) for node in soup.descendants):
        return True
    supported = set(
        "p div span br hr h1 h2 h3 h4 h5 h6 strong b em i s del a img ul ol li blockquote pre code table thead tbody tr th td".split()
    )
    if any(element.name not in supported for element in soup.find_all(True)):
        return True
    if soup.find(
        [
            "input",
            "iframe",
            "video",
            "audio",
            "svg",
            "math",
            "details",
            "script",
            "style",
        ]
    ):
        return True
    for element in soup.find_all(True):
        if element.get("style") or any(
            k.startswith("data-")
            and k not in {"data-upnote-tag", "data-file-id", "data-non-editable"}
            for k in element.attrs
        ):
            return True
        if any(not str(c).startswith("language-") for c in element.get("class", [])):
            return True
    return False


def _fingerprint(row) -> str:
    fields = (
        "id",
        "title",
        "html",
        "revision",
        "trashed",
        "deleted",
        "fileIds",
        "tagLinks",
        "space",
    )
    return hashlib.sha256(
        json.dumps(
            {key: row[key] for key in fields}, sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()


class UpNoteAdapter(NoteAdapter):
    endpoint = Endpoint.UPNOTE

    def __init__(self, config: AppConfig):
        self.data = Path(config.upnote_data_dir).expanduser().resolve()
        self.db = self.data / "upnote.sqlite3"
        self.root_title = config.upnote_notebook.strip()

    @contextmanager
    def _read(self):
        if not self.db.is_file():
            raise AdapterError("请选择包含 upnote.sqlite3 的 UpNote 数据目录。")
        try:
            with closing(
                sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=5)
            ) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                for table, required in SCHEMA.items():
                    columns = {
                        r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
                    }
                    if not required <= columns:
                        raise AdapterError(
                            f"UpNote 数据库结构不兼容：{table}；未执行写入。"
                        )
                yield conn
        except sqlite3.Error as exc:
            raise AdapterError(f"无法读取 UpNote 数据库：{exc}") from exc

    def _tree(self, conn):
        rows = {
            r["id"]: dict(r)
            for r in conn.execute(
                "SELECT * FROM notebooks WHERE COALESCE(deleted,0)=0 AND COALESCE(space,'')=''"
            )
        }
        roots = [
            r
            for r in rows.values()
            if not r["parent"]
            and (not self.root_title or r["title"] == self.root_title)
        ]
        if self.root_title and len(roots) > 1:
            raise AdapterError("UpNote 默认空间存在同名顶层笔记本，请先区分名称。")
        if not roots:
            if rows and not self.root_title:
                raise AdapterError("UpNote 存在父级缺失或循环的笔记本，未继续扫描。")
            return {}, {}
        paths = {}
        by_path = {}

        def visit(identifier, path, ancestors):
            if identifier in ancestors:
                raise AdapterError("UpNote 笔记本层级有循环。")
            if path in by_path:
                raise AdapterError(f"UpNote 笔记本路径重名：{path}")
            if rows[identifier]["locked"]:
                raise AdapterError("同步范围包含锁定笔记本，请先在 UpNote 中解锁。")
            paths[identifier] = path
            by_path[path] = identifier
            for row in rows.values():
                if row["parent"] == identifier:
                    name = row["title"] or ""
                    if not name or "/" in name or "\\" in name:
                        raise AdapterError("UpNote 笔记本名称不能映射为目录。")
                    visit(
                        row["id"],
                        "/".join(p for p in (path, name) if p),
                        ancestors | {identifier},
                    )

        for root in sorted(roots, key=lambda row: (row["title"] or "").casefold()):
            title = root["title"] or ""
            if not self.root_title and (not title or "/" in title or "\\" in title):
                raise AdapterError("UpNote 顶层笔记本名称不能映射为目录。")
            visit(root["id"], "" if self.root_title else title, set())
        if not self.root_title and set(rows) != set(paths):
            raise AdapterError("UpNote 存在父级缺失或循环的笔记本，未继续扫描。")
        return paths, by_path

    def _members(self, conn, paths):
        members = {}
        for identifier, path in paths.items():
            row = conn.execute(
                "SELECT content FROM lists WHERE id=? AND COALESCE(deleted,0)=0",
                ("notebooks_" + identifier,),
            ).fetchone()
            if row:
                for note_id in _array(row["content"]):
                    if note_id in members:
                        raise AdapterError(
                            "同一笔记位于同步范围的多个笔记本，请缩小同步范围。"
                        )
                    members[note_id] = path
        if not self.root_title:
            for row in conn.execute(
                "SELECT id FROM notes WHERE COALESCE(space,'')='' AND COALESCE(deleted,0)=0 AND COALESCE(trashed,0)=0 AND COALESCE(isTemplate,0)=0"
            ):
                members.setdefault(row["id"], "")
        return members

    def list_top_level_notebooks(self) -> list[str]:
        with self._read() as c:
            return sorted(
                {
                    row["title"]
                    for row in c.execute(
                        "SELECT title FROM notebooks WHERE COALESCE(deleted,0)=0 AND COALESCE(space,'')='' AND COALESCE(parent,'')=''"
                    )
                    if row["title"]
                },
                key=str.casefold,
            )

    def test_connection(self) -> str:
        with self._read() as c:
            paths, _ = self._tree(c)
            count = len(self._members(c, paths))
        suffix = (
            f"{count} 篇笔记"
            if paths or not self.root_title
            else "首次写入时创建笔记本"
        )
        return f"UpNote 默认空间 / {self.root_title or '全部笔记本'}：{suffix}；写入前须完全退出 UpNote。"

    def list_folders(self):
        with self._read() as c:
            return sorted(set(self._tree(c)[0].values()) | {""}, key=str.casefold)

    def list_notes(self):
        with self._read() as c:
            paths, _ = self._tree(c)
            result = []
            for identifier, folder in self._members(c, paths).items():
                self.check_cancelled()
                note = self._note(c, identifier, folder)
                if note:
                    result.append(note)
            return result

    def read_note(self, native_id):
        with self._read() as c:
            members = self._members(c, self._tree(c)[0])
            return (
                self._note(c, native_id, members[native_id])
                if native_id in members
                else None
            )

    def _note(self, c, identifier, folder):
        row = c.execute(
            "SELECT * FROM notes WHERE id=? AND COALESCE(deleted,0)=0 AND COALESCE(trashed,0)=0 AND COALESCE(isTemplate,0)=0",
            (identifier,),
        ).fetchone()
        if row is None:
            return None
        if row["space"]:
            raise AdapterError("UpNote 笔记空间与所选默认空间不一致。")
        soup = BeautifulSoup(row["html"] or "", "html.parser")
        first = soup.find()
        if (
            first
            and first.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
            and first.get_text() == row["title"]
        ):
            first.decompose()
        rich = _rich(soup)
        # Standalone native tag paragraphs represent metadata, not extra prose.
        for parent in list(soup.find_all(["p", "div"])):
            if parent.parent is None:
                continue
            anchors = parent.find_all("a", attrs={"data-upnote-tag": True})
            if (
                anchors
                and parent.get_text().strip()
                == " ".join(a.get_text() for a in anchors).strip()
            ):
                parent.decompose()
        for anchor in soup.find_all("a", attrs={"data-upnote-tag": True}):
            anchor.unwrap()
        listed = set(_array(row["fileIds"]))
        seen = set()
        assets = {}
        file_map = {}
        for tag, attr in [("img", "src"), ("a", "href")]:
            for element in soup.find_all(tag):
                url = urlparse(element.get(attr, ""))
                if url.hostname not in {
                    "localhost",
                    "127.0.0.1",
                } or not url.path.startswith(("/images/", "/files/")):
                    continue
                name = Path(unquote(url.path)).name
                if not FILE_NAME.fullmatch(name):
                    raise AdapterError("UpNote 附件路径无法识别，未继续同步。")
                stem, ext = name.rsplit(".", 1)
                file_id = stem + "__" + ext
                if file_id not in listed:
                    raise AdapterError("UpNote 附件正文与索引不一致。")
                record = c.execute(
                    "SELECT * FROM files WHERE id=? AND COALESCE(deleted,0)=0",
                    (file_id,),
                ).fetchone()
                path = (self.data / "images" / name).resolve()
                if (
                    not path.is_relative_to((self.data / "images").resolve())
                    or not record
                    or not path.is_file()
                ):
                    raise AdapterError(
                        "UpNote 附件未下载到本机，请先在 UpNote 中完成下载。"
                    )
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                filename = record["name"] or name
                assets[digest] = Asset(
                    digest,
                    filename,
                    len(raw),
                    source_ref=str(path),
                    media_type=mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                    _loader=path.read_bytes,
                )
                file_map[digest] = {"id": file_id, "local_name": name, "name": filename}
                element[attr] = canonical_asset_uri(digest, filename)
                seen.add(file_id)
        if listed - seen:
            rich = True  # Never discard a resource type that this adapter cannot map.
        tag_keys = _array(row["tagLinks"])
        titles = {
            r["title"].lstrip("#").casefold(): r["title"].lstrip("#")
            for r in c.execute(
                "SELECT title FROM tags WHERE COALESCE(deleted,0)=0 AND COALESCE(space,'')=''"
            )
        }
        tags = tuple(
            titles.get(key.lstrip("#").casefold(), key.lstrip("#")) for key in tag_keys
        )
        return Note(
            self.endpoint,
            identifier,
            "",
            row["title"] or "",
            folder,
            _md(str(soup)),
            tags,
            updated=int(row["updatedAt"] or 0),
            revision=str(row["revision"]),
            locator=identifier,
            assets=assets,
            native={
                "file_map": file_map,
                "upnote_fingerprint": _fingerprint(row),
                "upnote_html": row["html"],
                "upnote_rich": rich,
            },
        )

    def _guard(self):
        if is_upnote_running():
            raise AdapterError(
                "请使用 Ctrl+Q 完全退出 UpNote 后重新执行；程序不会强制关闭应用。"
            )

    def preflight_write(self, source):
        self._guard()
        super().preflight_write(source)
        if re.match(r"\A\ufeff?---\s*\r?\n", source.body):
            raise AdapterError(
                "UpNote 尚不支持无损保存 YAML 属性，请先选择不含 YAML 的笔记。"
            )
        if re.search(r"^\s*[-*+]\s+\[[ xX]\]", source.body, re.M) or _rich(
            BeautifulSoup(_html(source.body), "html.parser")
        ):
            raise AdapterError("此笔记包含尚未支持的待办或专属格式，未写入 UpNote。")
        for asset in source.assets.values():
            if asset.size > 20 * 1024 * 1024 or len(asset.load()) > 20 * 1024 * 1024:
                raise AdapterError("UpNote 附件不能超过 20 MiB。")

    @contextmanager
    def _write(self):
        self._guard()
        with self._read() as src:
            self._tree(src)
            backup = (
                self.data
                / "Note Sync Hub Backups"
                / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            )
            backup.mkdir(parents=True)
            with closing(sqlite3.connect(backup / "upnote.sqlite3")) as dest:
                src.backup(dest)
        created = []
        committed = False
        try:
            with closing(sqlite3.connect(self.db, timeout=10)) as c:
                c.row_factory = sqlite3.Row
                c.execute("BEGIN IMMEDIATE")
                self._guard()
                yield c, created
                c.commit()
                committed = True
        except sqlite3.Error as exc:
            raise AdapterError(
                f"UpNote 写入失败，数据库备份位于 {backup}：{exc}"
            ) from exc
        finally:
            if not committed:
                for path in created:
                    path.unlink(missing_ok=True)

    def _set_list(self, c, identifier, content):
        encoded = json.dumps(list(dict.fromkeys(content)), ensure_ascii=False)
        if c.execute("SELECT 1 FROM lists WHERE id=?", (identifier,)).fetchone():
            c.execute(
                "UPDATE lists SET content=?,synced=0,deleted=0,revision=COALESCE(revision,0)+1 WHERE id=?",
                (encoded, identifier),
            )
        else:
            c.execute(
                "INSERT INTO lists(id,content,synced,deleted,revision,syncedAt) VALUES(?,?,0,0,1,0)",
                (identifier, encoded),
            )

    def _folder(self, c, folder, now):
        paths, by_path = self._tree(c)
        parent = None
        prefix = [("", self.root_title)] if self.root_title else []
        for path, title in prefix + [
            ("/".join(folder.split("/")[: i + 1]), part)
            for i, part in enumerate(folder.split("/"))
            if part
        ]:
            if path in by_path:
                parent = by_path[path]
                continue
            identifier = _uuid7()
            c.execute(
                "INSERT INTO notebooks(id,title,parent,notes,childNotebooks,synced,deleted,inactive,locked,revision,createdAt,updatedAt,activatedAt) VALUES(?,?,?,?,?,0,0,0,0,1,?,?,?)",
                (identifier, title, parent, "[]", "[]", now, now, now),
            )
            active = c.execute(
                "SELECT content FROM lists WHERE id='activeNotebooks'"
            ).fetchone()
            self._set_list(
                c,
                "activeNotebooks",
                (_array(active["content"]) if active else []) + [identifier],
            )
            self._set_list(c, "notebooks_" + identifier, [])
            parent = identifier
        return parent

    def _check_existing(self, c, existing):
        if existing is None:
            return
        members = self._members(c, self._tree(c)[0])
        row = c.execute(
            "SELECT * FROM notes WHERE id=?", (existing.native_id,)
        ).fetchone()
        if (
            not row
            or existing.native_id not in members
            or _fingerprint(row) != existing.native.get("upnote_fingerprint")
            or members[existing.native_id] != existing.folder
        ):
            raise AdapterError("UpNote 目标在预览后发生变化，请重新预览。")

    def _tags(self, c, soup, tags, now):
        tag_keys = {tag.casefold() for tag in tags}
        for tag in tags:
            key = tag.casefold()
            found = c.execute(
                "SELECT id,title FROM tags WHERE COALESCE(deleted,0)=0 AND COALESCE(space,'')='' "
            ).fetchall()
            if not any(r["title"].lstrip("#").casefold() == key for r in found):
                c.execute(
                    "INSERT INTO tags(id,title,notes,revision,synced,deleted,inactive,createdAt,updatedAt,activatedAt) VALUES(?,?,'[]',1,0,0,0,?,?,?)",
                    (_uuid7(), tag, now, now, now),
                )
            linked = False
            pattern = re.compile(r"(?<!\w)#" + re.escape(tag) + r"(?![\w/])", re.I)
            for text in list(soup.find_all(string=pattern)):
                if text.find_parent(["a", "code", "pre"]):
                    continue
                paragraph = text.find_parent(["p", "div"])
                words = paragraph.get_text().split() if paragraph else []
                if words and all(
                    word.startswith("#") and word[1:].casefold() in tag_keys
                    for word in words
                ):
                    # Keep authored tag-only lines as text. A separate native
                    # metadata paragraph below can be stripped on readback.
                    continue
                parts = []
                offset = 0
                for match in pattern.finditer(str(text)):
                    parts.append(NavigableString(str(text)[offset : match.start()]))
                    anchor = soup.new_tag("a", href="#")
                    anchor["data-upnote-tag"] = "#" + tag
                    anchor.string = match.group()
                    parts.append(anchor)
                    offset = match.end()
                    linked = True
                parts.append(NavigableString(str(text)[offset:]))
                text.replace_with(*parts)
            if not linked:
                p = soup.new_tag("p")
                a = soup.new_tag("a", href="#")
                a["data-upnote-tag"] = "#" + tag
                a.string = "#" + tag
                p.append(a)
                soup.append(p)

    def upsert_note(self, source, existing, folder, global_id):
        self.preflight_write(source)
        folder = normalize_folder(folder)
        if any(p in {".", ".."} for p in folder.split("/")):
            raise AdapterError("UpNote 目标目录无效。")
        preserve = (
            existing is not None
            and source.body == existing.body
            and source.tags == existing.tags
        )
        if existing and existing.native.get("upnote_rich") and not preserve:
            raise AdapterError(
                "原 UpNote 笔记含专属格式或未识别附件，不能无损覆盖；请先在 UpNote 中转换格式。"
            )
        now = int(time.time() * 1000)
        identifier = existing.native_id if existing else _uuid7()
        with self._write() as (c, created):
            self._check_existing(c, existing)
            notebook = self._folder(c, folder, now)
            mapping = {}
            old = existing.native.get("file_map", {}) if existing else {}
            for digest, asset in source.assets.items():
                if digest in old:
                    mapping[digest] = old[digest]
                    continue
                ext = Path(asset.filename).suffix.lstrip(".").lower() or "bin"
                if not re.fullmatch("[a-z0-9]+", ext):
                    raise AdapterError("附件扩展名无法识别。")
                unique = _uuid7()
                local = unique + "." + ext
                file_id = unique + "__" + ext
                path = self.data / "images" / local
                path.parent.mkdir(exist_ok=True)
                with path.open("xb") as handle:
                    handle.write(asset.load())
                created.append(path)
                c.execute(
                    "INSERT INTO files(id,name,createdAt,updatedAt,revision,synced,deleted) VALUES(?,?,?,?,1,0,0)",
                    (file_id, asset.filename, now, now),
                )
                mapping[digest] = {"id": file_id, "local_name": local}
            if preserve:
                soup = BeautifulSoup(existing.native["upnote_html"], "html.parser")
                first = soup.find()
                if (
                    first
                    and first.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
                    and first.get_text() == existing.title
                ):
                    first.string = source.title
                else:
                    soup.insert(
                        0,
                        BeautifulSoup(
                            "<h2>" + escape(source.title) + "</h2>", "html.parser"
                        ),
                    )
                html = str(soup)
                prior = c.execute(
                    "SELECT fileIds,firstImage,filesCount FROM notes WHERE id=?",
                    (identifier,),
                ).fetchone()
                file_ids = _array(prior["fileIds"])
                first_image = prior["firstImage"]
                count = prior["filesCount"]
            else:
                soup = BeautifulSoup(_html(source.body), "html.parser")
                first_image = ""
                file_ids = []
                count = 0
                for tag, attr in [("img", "src"), ("a", "href")]:
                    for el in soup.find_all(tag):
                        digest = canonical_asset_digest(el.get(attr, ""))
                        if digest is None:
                            continue
                        if digest not in mapping:
                            raise AdapterError("正文引用了缺失附件。")
                        record = mapping[digest]
                        file_ids.append(record["id"])
                        target = (
                            "http://localhost:9425/"
                            + ("images/" if tag == "img" else "files/")
                            + record["local_name"]
                        )
                        el[attr] = target
                        if tag == "img":
                            el["width"] = "100%"
                            first_image = first_image or target
                        else:
                            count += 1
                            el["spellcheck"] = "false"
                            el["data-file-id"] = record["id"]
                            el["data-non-editable"] = "true"
                self._tags(c, soup, source.tags, now)
                html = "<h2>" + escape(source.title) + "</h2>" + str(soup)
            plain = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
            summary = _md(str(soup))[:200]
            values = (
                source.title,
                html,
                plain,
                summary,
                json.dumps(list(dict.fromkeys(file_ids))),
                first_image,
                count,
                now,
                json.dumps([t.casefold() for t in source.tags], ensure_ascii=False),
            )
            if existing:
                c.execute(
                    "UPDATE notes SET title=?,html=?,text=?,summary=?,fileIds=?,firstImage=?,filesCount=?,updatedAt=?,tagLinks=?,revision=COALESCE(revision,0)+1,synced=0 WHERE id=?",
                    (*values, identifier),
                )
            else:
                c.execute(
                    "INSERT INTO notes(id,title,html,text,summary,fileIds,firstImage,filesCount,updatedAt,tagLinks,createdAt,revision,synced,trashed,deleted,noteLinks,notebookLinks,hasTodo,bookmarked,pinned,highlighted) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,0,0,0,'[]','[]',0,0,0,0)",
                    (identifier, *values, now),
                )
            paths, _ = self._tree(c)
            for book in paths:
                key = "notebooks_" + book
                row = c.execute(
                    "SELECT content FROM lists WHERE id=?", (key,)
                ).fetchone()
                members = _array(row["content"]) if row else []
                updated = members
                if book == notebook and identifier not in members:
                    updated = members + [identifier]
                elif book != notebook and identifier in members:
                    updated = [n for n in members if n != identifier]
                if updated != members:
                    self._set_list(c, key, updated)
        return identifier

    def matches_written(self, actual, source, folder):
        return super().matches_written(
            actual, replace(source, body=_md(_html(source.body))), folder
        )

    def move_to_trash(self, note):
        with self._write() as (c, _):
            self._check_existing(c, note)
            c.execute(
                "UPDATE notes SET trashed=1,synced=0,revision=COALESCE(revision,0)+1,updatedAt=? WHERE id=?",
                (int(time.time() * 1000), note.native_id),
            )
