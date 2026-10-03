"""Keep Markdown properties outside SiYuan's visible block content."""
from __future__ import annotations

import base64
import binascii
import json
import re

import yaml

from .metadata import FRONTMATTER_RE

PROPERTY_PREFIX = "custom-obsidian-"
FRONTMATTER_ATTR = PROPERTY_PREFIX + "frontmatter-base64"


def separate_properties(content: str) -> tuple[str, dict[str, str]]:
    match = FRONTMATTER_RE.match(content)
    if not match:
        return content, {}
    try:
        values = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return content, {}
    if not isinstance(values, dict):
        return content, {}
    end = match.end()
    while end < len(content) and content[end] in "\r\n":
        end += 1
    prefix = content[:end]
    attrs = {
        FRONTMATTER_ATTR: base64.b64encode(prefix.encode("utf-8")).decode("ascii"),
        PROPERTY_PREFIX + "frontmatter": prefix.strip(),
        PROPERTY_PREFIX + "properties": json.dumps(values, ensure_ascii=True, default=str),
    }
    for key, value in values.items():
        name = PROPERTY_PREFIX + str(key).lower().replace("_", "-")
        if re.fullmatch(r"custom-obsidian-[a-z][a-z0-9-]*", name) and name not in attrs:
            attrs[name] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return content[end:], attrs


def restore_properties(content: str, attrs: dict[str, str]) -> str:
    raw = attrs.get(FRONTMATTER_ATTR)
    if not raw:
        return content
    try:
        prefix = base64.b64decode(raw, validate=True).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        raise ValueError("思源文档中保存的原始 YAML 属性损坏，已停止同步。") from None
    if not FRONTMATTER_RE.match(prefix):
        raise ValueError("思源文档中的原始 YAML 属性格式无效，已停止同步。")
    return prefix + content


def comparable_html(content: str) -> str:
    # Kernel rendering assigns fresh block IDs and timestamps on every call.
    content = re.sub(r' (?:id="[0-9]{14}-[a-z0-9]{7}"|updated="[0-9]{14}")', "", content)
    parts = re.split(r'(<(?:pre|code)\b[^>]*>.*?</(?:pre|code)>)', content, flags=re.DOTALL)
    for index in range(0, len(parts), 2):
        # SiYuan exports cursor separators and placeholders for empty list items.
        parts[index] = parts[index].replace('\u200b', '')
        parts[index] = re.sub('>\u200d<', '><', parts[index]).replace('<p></p>\n', '')
    return ''.join(parts).strip()
