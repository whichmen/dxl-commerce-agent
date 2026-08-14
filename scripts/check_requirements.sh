#!/usr/bin/env bash
set -u

MODE="${1:-core}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_PY_BIN="python3"
if [[ -x "${PROJECT_DIR}/.venv/bin/python" ]]; then
  DEFAULT_PY_BIN="${PROJECT_DIR}/.venv/bin/python"
fi
PYTHON_BIN="${CLAWBOT_PYTHON_BIN:-${DEFAULT_PY_BIN}}"
missing=0

ok() {
  printf '[OK]   %s\n' "$1"
}

fail() {
  printf '[MISS] %s\n' "$1" >&2
  missing=$((missing + 1))
}

check_command() {
  local command_name="$1"
  local label="$2"
  if command -v "${command_name}" >/dev/null 2>&1; then
    ok "${label}: $(command -v "${command_name}")"
  else
    fail "${label} (${command_name})"
  fi
}

check_command "${PYTHON_BIN}" "Python 3.11+"
if command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  if "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' ; then
    ok "Python version is supported"
  else
    fail "Python must be 3.11 or newer"
  fi
  if "${PYTHON_BIN}" -c 'import fastapi, pydantic, playwright, yaml, PIL, Crypto' >/dev/null 2>&1; then
    ok "Python runtime dependencies"
  else
    fail "Python dependencies (run: python -m pip install -e .)"
  fi
fi

check_command curl "curl"
check_command jq "jq"
check_command node "Node.js"
check_command npm "npm"
check_command openclaw "OpenClaw CLI"

if [[ "${MODE}" == "workers" || "${MODE}" == "all" ]]; then
  chrome_bin="${CHROME_BIN:-google-chrome}"
  check_command "${chrome_bin}" "Chrome/Chromium for CDP workers"
fi

if [[ "${MODE}" == "mobile" || "${MODE}" == "all" ]]; then
  check_command adb "Android Debug Bridge"
  check_command java "JDK"
  if command -v appium >/dev/null 2>&1; then
    ok "Appium: $(command -v appium)"
  else
    printf '[INFO] Appium not found globally; run workers/scripts/install_appium_runtime.sh\n'
  fi
fi

if ((missing > 0)); then
  printf '\n%d required check(s) are missing for mode: %s\n' "${missing}" "${MODE}" >&2
  exit 1
fi

printf '\nAll required checks passed for mode: %s\n' "${MODE}"
