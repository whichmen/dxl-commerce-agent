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
export CLAWBOT_WECOM_HUB_CONFIG="${CLAWBOT_WECOM_HUB_CONFIG:-${PROJECT_DIR}/examples/wecom_hub.apps.example.json}"
DEFAULT_PY_BIN="python3"
if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  DEFAULT_PY_BIN="${PROJECT_DIR}/.venv/bin/python"
fi
exec "${CLAWBOT_PYTHON_BIN:-${DEFAULT_PY_BIN}}" -m uvicorn kefu_clawbot.wecom_hub_main:app \
  --host "${CLAWBOT_WECOM_HUB_HOST:-127.0.0.1}" \
  --port "${CLAWBOT_WECOM_HUB_PORT:-18081}"
