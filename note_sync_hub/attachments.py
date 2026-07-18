from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, unquote, urlparse


MARKDOWN_LINK_RE = re.compile(
    r"(?P<bang>!)?\[(?P<label>[^\]\r\n]*)\]\((?P<destination><[^>\r\n]+>|[^)\r\n]*)\)"
)
WIKI_LINK_RE = re.compile(r"(?P<bang>!)?\[\[(?P<inside>[^\]\r\n]+)\]\]")
HTML_LOCAL_RE = re.compile(
    r"<(?:img|audio|video|source|a)\b[^>]*?\b(?:src|href)\s*=\s*"
    r"(?P<quote>[\"'])(?P<target>.*?)(?P=quote)[^>]*>",
    re.IGNORECASE,
)
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)[^\r\n]*?(?P=ticks)")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")

IMAGE_EXTENSIONS = {
    ".apng", ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico",
    ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp",
}
CANONICAL_ASSET_SCHEME = "notesync-asset"
CANONICAL_ASSET_RE = re.compile(r"notesync-asset://(?P<digest>[a-fA-F0-9]{64})/(?P<name>[^\s)\]>'\"]+)")


@dataclass(frozen=True)
class AttachmentReference:
    start: int
    end: int
    target: str
    kind: str
    label: str = ""
    embedded: bool = False
    original: str = ""

    def render_joplin(self, resource_id: str, path: Path) -> str:
        destination = f":/{resource_id}"
        if self.kind == "html":
            return destination
        label = self.label.strip() or path.name
        if self.kind == "markdown":
            prefix = "!" if self.embedded else ""
            return f"{prefix}[{label}]({destination})"
        if self.embedded and path.suffix.casefold() in IMAGE_EXTENSIONS:
            return f"![{label}]({destination})"
        return f"[{label}]({destination})"

    def render_target(self, target: str, filename: str = "") -> str:
        if self.kind == "html":
            return target
        label = self.label.strip() or filename or Path(unquote(target)).name
        if self.kind == "markdown":
            prefix = "!" if self.embedded else ""
            return f"{prefix}[{label}]({target})"
        prefix = "!" if self.embedded else ""
        return f"{prefix}[{label}]({target})"


@dataclass(frozen=True)
class ResolvedAttachment:
    reference: AttachmentReference
    path: Path


@dataclass(frozen=True)
class AttachmentIssue:
    reference: AttachmentReference
    message: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_asset_uri(digest: str, filename: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9]{64}", digest or ""):
        raise ValueError("附件摘要必须是 64 位 SHA-256。")
    safe_name = quote(Path(filename or "attachment.bin").name, safe="._-~")
    return f"{CANONICAL_ASSET_SCHEME}://{digest.lower()}/{safe_name}"


def canonical_asset_digest(target: str) -> Optional[str]:
    match = CANONICAL_ASSET_RE.fullmatch((target or "").strip())
    return match.group("digest").lower() if match else None


def _fenced_code_ranges(content: str) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    fence_character = ""
    fence_length = 0
    fence_start = 0
    offset = 0
    for line in content.splitlines(keepends=True):
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if not fence_character:
                fence_character = marker[0]
                fence_length = len(marker)
                fence_start = offset
            elif marker[0] == fence_character and len(marker) >= fence_length:
                ranges.append((fence_start, offset + len(line)))
                fence_character = ""
                fence_length = 0
        offset += len(line)
    if fence_character:
        ranges.append((fence_start, len(content)))
    return ranges


def _ignored_ranges(content: str) -> List[Tuple[int, int]]:
    ranges = _fenced_code_ranges(content)
    ranges.extend((match.start(), match.end()) for match in INLINE_CODE_RE.finditer(content))
    return ranges


def _inside_ranges(start: int, end: int, ranges: Sequence[Tuple[int, int]]) -> bool:
    return any(start < ignored_end and end > ignored_start for ignored_start, ignored_end in ranges)


def _markdown_destination(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1:value.index(">")].strip()
    # CommonMark paths containing literal spaces must be enclosed in angle brackets.
    # Anything after whitespace is therefore a Markdown link title.
    return value.split(maxsplit=1)[0] if value else ""


def find_attachment_references(content: str) -> List[AttachmentReference]:
    ignored = _ignored_ranges(content or "")
    references: List[AttachmentReference] = []

    for match in MARKDOWN_LINK_RE.finditer(content or ""):
        if _inside_ranges(match.start(), match.end(), ignored):
            continue
        target = _markdown_destination(match.group("destination"))
        references.append(AttachmentReference(
            start=match.start(),
            end=match.end(),
            target=target,
            kind="markdown",
            label=match.group("label") or "",
            embedded=bool(match.group("bang")),
            original=match.group(0),
        ))

    for match in WIKI_LINK_RE.finditer(content or ""):
        if _inside_ranges(match.start(), match.end(), ignored):
            continue
        inside = match.group("inside")
        target, separator, alias = inside.partition("|")
        references.append(AttachmentReference(
            start=match.start(),
            end=match.end(),
            target=target.strip(),
            kind="wiki",
            label=alias.strip() if separator else "",
            embedded=bool(match.group("bang")),
            original=match.group(0),
        ))

    occupied = [(item.start, item.end) for item in references]
    for match in HTML_LOCAL_RE.finditer(content or ""):
        start, end = match.span("target")
        if _inside_ranges(start, end, ignored) or _inside_ranges(start, end, occupied):
            continue
        references.append(AttachmentReference(
            start=start,
            end=end,
            target=match.group("target").strip(),
            kind="html",
            embedded=match.group(0).lstrip().lower().startswith(("<img", "<audio", "<video", "<source")),
            original=match.group("target"),
        ))

    return sorted(references, key=lambda item: item.start)


def normalized_local_target(reference: AttachmentReference) -> Optional[str]:
    value = unquote((reference.target or "").strip()).replace("\\", "/")
    if not value or value.startswith("#") or value.startswith(":/"):
        return None
    parsed = urlparse(value)
    if parsed.scheme.casefold() in {"http", "https", "data", "mailto", "obsidian", "ftp", CANONICAL_ASSET_SCHEME}:
        return None
    if parsed.scheme and not WINDOWS_ABSOLUTE_RE.match(value):
        return None
    value = value.split("#", 1)[0].split("?", 1)[0].strip()
    return value or None


def is_attachment_candidate(reference: AttachmentReference) -> bool:
    target = normalized_local_target(reference)
    if not target:
        return False
    suffix = Path(target).suffix.casefold()
    if suffix == ".md":
        return False
    if reference.kind == "markdown" and reference.embedded:
        return True
    if reference.kind == "html" and reference.embedded:
        return True
    # Obsidian note transclusions such as ![[Meeting]] have no extension and
    # must remain note links, while [[manual.pdf]] is an attachment.
    return bool(suffix)


def attachment_references(content: str) -> List[AttachmentReference]:
    return [item for item in find_attachment_references(content) if is_attachment_candidate(item)]


def replace_with_joplin_resources(
    content: str,
    resolved: Iterable[ResolvedAttachment],
    resource_ids_by_path: Dict[Path, str],
) -> str:
    result = content
    replacements = []
    for item in resolved:
        resource_id = resource_ids_by_path.get(item.path.resolve())
        if not resource_id:
            continue
        replacements.append((
            item.reference.start,
            item.reference.end,
            item.reference.render_joplin(resource_id, item.path),
        ))
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def replace_joplin_resource_links(content: str, local_links: Dict[str, str]) -> str:
    result = content
    for resource_id, local_link in local_links.items():
        result = re.sub(
            rf":/{re.escape(resource_id)}\b",
            lambda _match, link=local_link: link,
            result,
            flags=re.IGNORECASE,
        )
    return result


def replace_reference_targets(
    content: str,
    replacements: Iterable[Tuple[AttachmentReference, str, str]],
) -> str:
    result = content
    rendered = [
        (reference.start, reference.end, reference.render_target(target, filename))
        for reference, target, filename in replacements
    ]
    for start, end, replacement in sorted(rendered, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def replace_canonical_asset_uris(content: str, targets_by_digest: Dict[str, str]) -> str:
    def replacement(match: re.Match) -> str:
        digest = match.group("digest").lower()
        return targets_by_digest.get(digest, match.group(0))

    return CANONICAL_ASSET_RE.sub(replacement, content or "")
