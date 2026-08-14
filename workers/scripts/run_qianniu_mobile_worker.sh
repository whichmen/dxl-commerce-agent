#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
STORE_ID="${STORE_ID:-taobao_store_example}"
STORE_NAME="${STORE_NAME:-示例淘宝店铺}"
DECISION_URL="${DECISION_URL:-http://127.0.0.1:18080}"
DECISION_KEY="${DECISION_KEY:-}"
STATE_DB="${STATE_DB:-${ROOT_DIR}/state/qianniu_mobile_worker.db}"
DECISION_TIMEOUT_SEC="${DECISION_TIMEOUT_SEC:-75}"
MIN_REPLY_INTERVAL_SEC="${MIN_REPLY_INTERVAL_SEC:-0.8}"
FALLBACK_TEXT="${FALLBACK_TEXT:-稍等}"
POLL_INTERVAL_SEC="${POLL_INTERVAL_SEC:-1.2}"
OPEN_CHAT_WAIT_SEC="${OPEN_CHAT_WAIT_SEC:-0.8}"
BACK_TO_LIST_WAIT_SEC="${BACK_TO_LIST_WAIT_SEC:-0.4}"

MOBILE_UDID="${MOBILE_UDID:-}"
APPIUM_PORT="${APPIUM_PORT:-4723}"
APPIUM_SERVER_URL="${APPIUM_SERVER_URL:-http://127.0.0.1:${APPIUM_PORT}}"
APPIUM_HOME="${APPIUM_HOME:-${ROOT_DIR}/.runtime/appium/home}"
APPIUM_BIN_DEFAULT="${ROOT_DIR}/.runtime/appium/node_modules/.bin/appium"
APPIUM_BIN="${APPIUM_BIN:-${APPIUM_BIN_DEFAULT}}"
APPIUM_LOG="${APPIUM_LOG:-/tmp/appium_${APPIUM_PORT}.log}"
ANDROID_HOME="${ANDROID_HOME:-/usr/lib/android-sdk}"
ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/usr/lib/android-sdk}"

mkdir -p "$(dirname "${STATE_DB}")"
mkdir -p "$(dirname "${APPIUM_LOG}")"
mkdir -p "${APPIUM_HOME}"

if [ -z "${MOBILE_UDID}" ]; then
  echo "[ERR] MOBILE_UDID is required (example: emulator-5554)" >&2
  exit 2
fi

if [ ! -x "${APPIUM_BIN}" ]; then
  APPIUM_BIN="$(command -v appium || true)"
fi
if [ -z "${APPIUM_BIN}" ] || [ ! -x "${APPIUM_BIN}" ]; then
  echo "[ERR] appium binary not found. Run scripts/install_appium_runtime.sh first." >&2
  exit 2
fi

appium_health() {
  curl -fsS "${APPIUM_SERVER_URL%/}/status" >/dev/null 2>&1
}

if ! appium_health; then
  echo "[INFO] starting appium server at ${APPIUM_SERVER_URL}" >&2
  APPIUM_HOME="${APPIUM_HOME}" \
  ANDROID_HOME="${ANDROID_HOME}" \
  ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT}" \
  nohup "${APPIUM_BIN}" \
    --address 127.0.0.1 \
    --port "${APPIUM_PORT}" \
    --base-path / \
    --log "${APPIUM_LOG}" \
    >/dev/null 2>&1 &
  for _ in $(seq 1 25); do
    if appium_health; then
      break
    fi
    sleep 0.4
  done
fi

if ! appium_health; then
  echo "[ERR] appium server not ready: ${APPIUM_SERVER_URL}" >&2
  exit 2
fi

export APPIUM_HOME
export ANDROID_HOME
export ANDROID_SDK_ROOT

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/qianniu_mobile_worker.py" \
  --udid "${MOBILE_UDID}" \
  --appium-server-url "${APPIUM_SERVER_URL}" \
  --store-id "${STORE_ID}" \
  --store-name "${STORE_NAME}" \
  --decision-url "${DECISION_URL}" \
  --state-db "${STATE_DB}" \
  --decision-timeout-sec "${DECISION_TIMEOUT_SEC}" \
  --min-reply-interval-sec "${MIN_REPLY_INTERVAL_SEC}" \
  --fallback-text "${FALLBACK_TEXT}" \
  --poll-interval-sec "${POLL_INTERVAL_SEC}" \
  --open-chat-wait-sec "${OPEN_CHAT_WAIT_SEC}" \
  --back-to-list-wait-sec "${BACK_TO_LIST_WAIT_SEC}"
