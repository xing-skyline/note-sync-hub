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
HTML_HEADER_RE = re.compile(
    r"\A(?:<!--[ \t]*(?:notesynchub|notebridge)_(?:id|sync_time|source|version):"
    r"[^\r\n]*?-->[ \t]*(?:\r?\n|$))+", re.IGNORECASE,
)
FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
SYNC_FIELD_RE = re.compile(r"^(?:notesynchub|notebridge)_", re.IGNORECASE)
SYNC_FRONTMATTER_FIELD_RE = re.compile(
    r"^(?:notesynchub|notebridge)_(id|sync_time|source|version):[ \t]*(.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


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


def _fallback_sync_values(frontmatter: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for key, raw_value in SYNC_FRONTMATTER_FIELD_RE.findall(frontmatter or ""):
        try:
            parsed = yaml.safe_load(raw_value)
        except yaml.YAMLError:
            parsed = raw_value
        if isinstance(parsed, (dict, list)):
            parsed = raw_value
        values.setdefault(key.casefold(), str(parsed or ""))
    return values


def _orphan_frontmatter_tags(frontmatter: str) -> List[str]:
    cleaned = _strip_sync_frontmatter(frontmatter)
    if not cleaned:
        return []
    try:
        value = yaml.safe_load(cleaned)
    except yaml.YAMLError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip().lstrip("#") for item in value if str(item).strip().lstrip("#")]


def extract_joplin_metadata(content: str) -> Optional[SyncMetadata]:
    header = HTML_HEADER_RE.match(content or "")
    values = {key.casefold(): value.strip() for key, value in HTML_FIELD_RE.findall(header.group() if header else "")}
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
    fallback = _fallback_sync_values(frontmatter)
    global_id = str(
        values.get("notesynchub_id")
        or values.get("notebridge_id")
        or fallback.get("id")
        or ""
    )
    if not global_id:
        return None
    return SyncMetadata(
        global_id=global_id,
        synced_at=str(
            values.get("notesynchub_sync_time")
            or values.get("notebridge_sync_time")
            or fallback.get("sync_time")
            or ""
        ),
        source=str(
            values.get("notesynchub_source")
            or values.get("notebridge_source")
            or fallback.get("source")
            or ""
        ),
        version=str(
            values.get("notesynchub_version")
            or values.get("notebridge_version")
            or fallback.get("version")
            or "1"
        ),
    )


def obsidian_metadata_needs_repair(content: str) -> bool:
    frontmatter, _body = split_frontmatter(content)
    return bool(
        frontmatter
        and not _frontmatter_mapping(frontmatter)
        and _fallback_sync_values(frontmatter).get("id")
    )


def strip_joplin_metadata(content: str) -> str:
    header = HTML_HEADER_RE.match(content or "")
    if not header or not extract_joplin_metadata(content):
        return content or ""
    # Old writers inserted exactly one blank separator after the marker lines.
    return re.sub(r"\A\r?\n", "", content[header.end():], count=1)


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
    if not extract_obsidian_metadata(content):
        return content or ""
    if _orphan_frontmatter_tags(frontmatter):
        return body
    match = FRONTMATTER_RE.match(content)
    # Remove only known, top-level legacy fields. Never dump/reformat user YAML.
    cleaned = "".join(
        line for line in frontmatter.splitlines(keepends=True)
        if not SYNC_FRONTMATTER_FIELD_RE.fullmatch(line.rstrip("\r\n"))
    )
    if not cleaned.strip():
        return body
    cleaned = re.sub(r"\r?\n\Z", "", cleaned, count=1)
    return content[:match.start(1)] + cleaned + content[match.end(1):]


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
    orphan_tags = _orphan_frontmatter_tags(frontmatter)
    if orphan_tags:
        cleaned = ""
    lines = cleaned.splitlines() if cleaned else []
    tag_list = [str(tag).strip() for tag in tags if str(tag).strip()] if tags is not None else []
    if not tag_list:
        tag_list = orphan_tags
    if tag_list and not any(line.lstrip().startswith("tags:") for line in lines):
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
        if not tags:
            tags = _orphan_frontmatter_tags(frontmatter)
    else:
        tags = _orphan_frontmatter_tags(frontmatter)
    tags.extend(re.findall(r"(?<![\w/])#([^\s#.,，。！？!?:：;；]+)", body))
    return list(dict.fromkeys(tag.strip().lstrip("#") for tag in tags if tag.strip().lstrip("#")))


def strip_platform_metadata(content: str, source: str) -> str:
    if source == "joplin":
        return strip_joplin_metadata(content)
    if source == "obsidian":
        return strip_obsidian_metadata(content)
    return content or ""


def legacy_canonical_body(content: str, source: str) -> str:
    """Reproduce version-2 hashing only; never use this text for writing notes."""
    from .attachments import canonical_asset_digest, find_attachment_references

    if source == "joplin":
        content = re.sub(r"\A[ \t]*(?:\r?\n)+", "", HTML_FIELD_RE.sub("", content))
    elif source == "obsidian":
        frontmatter, body = split_frontmatter(content)
        if frontmatter:
            cleaned = "" if _orphan_frontmatter_tags(frontmatter) else _strip_sync_frontmatter(frontmatter, strip_tags=True)
            content = (f"---\n{cleaned}\n---\n" if cleaned else "") + body.lstrip("\r\n")
    # The old attachment renderer also discarded Markdown captions and spacing.
    for reference in reversed(find_attachment_references(content)):
        if reference.kind == "markdown" and canonical_asset_digest(reference.target):
            from urllib.parse import unquote
            label = reference.label.strip() or unquote(reference.target.rsplit("/", 1)[-1])
            prefix = "!" if reference.embedded else ""
            content = content[:reference.start] + f"{prefix}[{label}]({reference.target})" + content[reference.end:]
    return content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
