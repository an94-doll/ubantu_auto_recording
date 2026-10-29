#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$ROOT"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    printf '%s\n' '未找到 .venv/bin/python，请按 README 创建 Ubuntu 虚拟环境并安装依赖。' >&2
    exit 1
fi
# Desktop autostart does not source ~/.bashrc. Set the MVS runtime explicitly.
MVS_ROOT="${MVS_ROOT:-/opt/MVS}"
export MVCAM_COMMON_RUNENV="${MVCAM_COMMON_RUNENV:-$MVS_ROOT/lib}"
export LD_LIBRARY_PATH="$MVCAM_COMMON_RUNENV/64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$PYTHON" -m slicing_recorder "$@"
