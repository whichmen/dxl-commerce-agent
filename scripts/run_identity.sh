#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

if [[ -f "${PROJECT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_DIR}/.env"
  set +a
fi

export PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
DEFAULT_PY_BIN="python3"
if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  DEFAULT_PY_BIN="${PROJECT_DIR}/.venv/bin/python"
fi
exec "${CLAWBOT_PYTHON_BIN:-${DEFAULT_PY_BIN}}" -m uvicorn kefu_identity_service.main:app \
  --host "${IDENTITY_HOST:-127.0.0.1}" \
  --port "${IDENTITY_PORT:-18082}"
