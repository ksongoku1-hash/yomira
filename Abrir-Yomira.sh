#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ ! -x "$root/.venv/bin/python" ]]; then "$root/Instalar-Yomira.sh"; fi
exec "$root/.venv/bin/python" "$root/scripts/open-yomira-linux.py"
