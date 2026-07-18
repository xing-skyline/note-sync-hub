from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


HTML_FIELD_RE = re.compile(
    r"<!--\s*(?:notesynchub|notebridge)_(id|sync_time|source|version):\s*(.*?)\s*-->",
    re.IGNORECASE,
)
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
SYNC_FIELD_RE = re.compile(r"^(?:notesynchub|notebridge)_", re.IGNORECASE)


@dataclass(frozen=True)
class SyncMetadata:
    global_id: str
    synced_at: str
    source: str
    version: str = "1"

    @classmethod
    def create(cls, source: str, global_id: str = "") -> "SyncMetadata":
        return cls(
            global_id=global_id or str(uuid.uuid4()),
            synced_at=datetime.now(timezone.utc).isoformat(),
            source=source,
        )


def split_frontmatter(content: str) -> Tuple[str, str]:
    match = FRONTMATTER_RE.match(content or "")
    if not match:
        return "", content or ""
    return match.group(1), (content or "")[match.end() :]


def _frontmatter_mapping(frontmatter: str) -> Dict[str, Any]:
    if not frontmatter.strip():
        return {}
    try:
        value = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return {}
    return value if isinstance(value, dict) else {}


def extract_joplin_metadata(content: str) -> Optional[SyncMetadata]:
    values = {key.casefold(): value.strip() for key, value in HTML_FIELD_RE.findall(content or "")}
    global_id = values.get("id", "")
    if not global_id:
        return None
    return SyncMetadata(
        global_id=global_id,
        synced_at=values.get("sync_time", ""),
        source=values.get("source", ""),
        version=values.get("version", "1"),
    )


def extract_obsidian_metadata(content: str) -> Optional[SyncMetadata]:
    frontmatter, _body = split_frontmatter(content)
    values = _frontmatter_mapping(frontmatter)
    global_id = str(values.get("notesynchub_id") or values.get("notebridge_id") or "")
    if not global_id:
        return None
    return SyncMetadata(
        global_id=global_id,
        synced_at=str(values.get("notesynchub_sync_time") or values.get("notebridge_sync_time") or ""),
        source=str(values.get("notesynchub_source") or values.get("notebridge_source") or ""),
        version=str(values.get("notesynchub_version") or values.get("notebridge_version") or "1"),
    )


def strip_joplin_metadata(content: str) -> str:
    cleaned = HTML_FIELD_RE.sub("", content or "")
    return re.sub(r"\A[ \t]*(?:\r?\n)+", "", cleaned)


def _strip_sync_frontmatter(frontmatter: str, *, strip_tags: bool = False) -> str:
    if strip_tags:
        values = _frontmatter_mapping(frontmatter)
        if values:
            kept = {
                key: value
                for key, value in values.items()
                if not SYNC_FIELD_RE.match(str(key)) and str(key).casefold() != "tags"
            }
            if not kept:
                return ""
            return yaml.safe_dump(
                kept,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ).strip()
    kept = [line for line in frontmatter.splitlines() if not SYNC_FIELD_RE.match(line.lstrip())]
    return "\n".join(kept).strip("\n")


def strip_obsidian_metadata(content: str) -> str:
    frontmatter, body = split_frontmatter(content)
    if not frontmatter:
        return content or ""
    # tags 已作为 Note.tags 单独比较和同步，不能再留在正文签名中，否则
    # 从 Joplin/思源写入的标签会让 Obsidian 永久显示为“正文有变化”。
    cleaned = _strip_sync_frontmatter(frontmatter, strip_tags=True)
    if not cleaned:
        return body.lstrip("\r\n")
    return f"---\n{cleaned}\n---\n{body.lstrip(chr(13) + chr(10))}"


def apply_joplin_metadata(content: str, metadata: SyncMetadata) -> str:
    body = strip_joplin_metadata(content).lstrip("\r\n")
    header = (
        f"<!-- notesynchub_id: {metadata.global_id} -->\n"
        f"<!-- notesynchub_sync_time: {metadata.synced_at} -->\n"
        f"<!-- notesynchub_source: {metadata.source} -->\n"
        f"<!-- notesynchub_version: {metadata.version} -->\n\n"
    )
    return header + body


def apply_obsidian_metadata(
    content: str,
    metadata: SyncMetadata,
    tags: Optional[Iterable[str]] = None,
) -> str:
    frontmatter, body = split_frontmatter(content)
    cleaned = _strip_sync_frontmatter(frontmatter)
    lines = cleaned.splitlines() if cleaned else []
    if tags is not None and not any(line.lstrip().startswith("tags:") for line in lines):
        tag_list = [str(tag).strip() for tag in tags if str(tag).strip()]
        if tag_list:
            dumped = yaml.safe_dump(tag_list, allow_unicode=True, default_flow_style=True).strip()
            lines.append("tags: " + dumped)
    lines.extend(
        [
            f"notesynchub_id: {metadata.global_id}",
            f"notesynchub_sync_time: '{metadata.synced_at}'",
            f"notesynchub_source: {metadata.source}",
            f"notesynchub_version: '{metadata.version}'",
        ]
    )
    return "---\n" + "\n".join(lines) + "\n---\n" + body.lstrip("\r\n")


def extract_obsidian_tags(content: str) -> List[str]:
    frontmatter, body = split_frontmatter(content)
    raw = _frontmatter_mapping(frontmatter).get("tags", [])
    if isinstance(raw, str):
        tags = [raw]
    elif isinstance(raw, list):
        tags = [str(value) for value in raw]
    else:
        tags = []
    tags.extend(re.findall(r"(?<![\w/])#([^\s#.,，。！？!?:：;；]+)", body))
    return list(dict.fromkeys(tag.strip().lstrip("#") for tag in tags if tag.strip().lstrip("#")))


def strip_platform_metadata(content: str, source: str) -> str:
    if source == "joplin":
        return strip_joplin_metadata(content)
    if source == "obsidian":
        return strip_obsidian_metadata(content)
    return content or ""
