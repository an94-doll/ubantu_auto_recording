#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$ROOT"
PYTHON="${RECORDER_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
    printf '找不到可执行的 Python：%s，请创建 .venv 或设置 RECORDER_PYTHON。\n' "$PYTHON" >&2
    exit 1
fi
# Desktop autostart does not source ~/.bashrc. Set the MVS runtime explicitly.
MVS_ROOT="${MVS_ROOT:-/opt/MVS}"
export MVCAM_COMMON_RUNENV="${MVCAM_COMMON_RUNENV:-$MVS_ROOT/lib}"
if [[ -n "${RECORDER_PYTHON:-}" ]]; then
    ENV_LIB="$(cd -- "$(dirname -- "$PYTHON")/../lib" && pwd)"
    export LD_LIBRARY_PATH="$MVCAM_COMMON_RUNENV/64:$ENV_LIB${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
else
    export LD_LIBRARY_PATH="$MVCAM_COMMON_RUNENV/64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
exec "$PYTHON" -m slicing_recorder "$@"
