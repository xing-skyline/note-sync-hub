"""Generate PyInstaller version resources from the project version."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


PRODUCT_NAME = "Note Sync Hub"
FILE_DESCRIPTION = "Note Sync Hub"
COMPANY_NAME = "xing-skyline"
ORIGINAL_FILENAME = "NoteSyncHub.exe"
LEGAL_COPYRIGHT = "Copyright © 2026 xing-skyline"


def read_project_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as pyproject_file:
        project = tomllib.load(pyproject_file).get("project", {})

    version = project.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"Missing [project].version in {pyproject_path}")
    return version.strip()


def windows_version(project_version: str) -> tuple[int, int, int, int]:
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", project_version):
        raise ValueError(
            "Windows version resources require one to four numeric version "
            f"components; got {project_version!r}"
        )

    components = [int(component) for component in project_version.split(".")]
    components.extend([0] * (4 - len(components)))
    if any(component > 65535 for component in components):
        raise ValueError("Windows version components must not exceed 65535")
    return (components[0], components[1], components[2], components[3])


def render_version_info(project_version: str) -> tuple[str, dict[str, str]]:
    numeric_version = windows_version(project_version)
    file_version = ".".join(str(component) for component in numeric_version)
    metadata = {
        "product_name": PRODUCT_NAME,
        "file_description": FILE_DESCRIPTION,
        "company_name": COMPANY_NAME,
        "file_version": file_version,
        "product_version": project_version,
        "original_filename": ORIGINAL_FILENAME,
        "legal_copyright": LEGAL_COPYRIGHT,
    }
    resource = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numeric_version},
    prodvers={numeric_version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', {COMPANY_NAME!r}),
          StringStruct('FileDescription', {FILE_DESCRIPTION!r}),
          StringStruct('FileVersion', {file_version!r}),
          StringStruct('LegalCopyright', {LEGAL_COPYRIGHT!r}),
          StringStruct('OriginalFilename', {ORIGINAL_FILENAME!r}),
          StringStruct('ProductName', {PRODUCT_NAME!r}),
          StringStruct('ProductVersion', {project_version!r})
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    return resource, metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project_version = read_project_version(args.pyproject)
    resource, metadata = render_version_info(project_version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(resource, encoding="utf-8", newline="\n")
    print(json.dumps(metadata))


if __name__ == "__main__":
    main()
