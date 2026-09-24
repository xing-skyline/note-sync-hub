#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
command -v uv >/dev/null || { echo "请先安装 uv：brew install uv"; exit 1; }
if [[ ! -x .venv/bin/python ]]; then uv venv --python 3.11 .venv; fi
UV_LINK_MODE=copy uv pip install --python .venv/bin/python -e .
