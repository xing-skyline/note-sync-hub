#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
[[ -x .venv/bin/python ]] || ./setup-macos.sh
exec .venv/bin/python -m note_sync_hub.gui "$@"
