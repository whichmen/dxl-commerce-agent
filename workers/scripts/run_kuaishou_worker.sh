#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
REMOTE_PORT="${REMOTE_PORT:-9225}"
STORE_ID="${STORE_ID:-kuaishou_store_example}"
STORE_NAME="${STORE_NAME:-示例快手店铺}"
DECISION_URL="${DECISION_URL:-http://127.0.0.1:18080}"
DECISION_KEY="${DECISION_KEY:-}"
STATE_DB="${STATE_DB:-${ROOT_DIR}/state/kuaishou_worker.db}"
DECISION_TIMEOUT_SEC="${DECISION_TIMEOUT_SEC:-75}"
MIN_REPLY_INTERVAL_SEC="${MIN_REPLY_INTERVAL_SEC:-0.8}"
FALLBACK_TEXT="${FALLBACK_TEXT:-稍等}"

mkdir -p "$(dirname "${STATE_DB}")"

TARGET_URL="${TARGET_URL:-https://im.kwaixiaodian.com/workbench}"
CHROME_LOG="${CHROME_LOG:-/tmp/chrome_kuaishou_${REMOTE_PORT}.log}"
CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-${ROOT_DIR}/chrome_user_data/kuaishou_example}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-Default}"

export PYTHON_BIN REMOTE_PORT STORE_ID STORE_NAME DECISION_URL DECISION_KEY STATE_DB
export DECISION_TIMEOUT_SEC MIN_REPLY_INTERVAL_SEC FALLBACK_TEXT
export TARGET_URL CHROME_LOG CHROME_USER_DATA_DIR CHROME_PROFILE_DIR

"${ROOT_DIR}/scripts/start_chrome_9222.sh"

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/kuaishou_auto_reply.py" \
  --ports "${REMOTE_PORT}" \
  --store-id "${STORE_ID}" \
  --store-name "${STORE_NAME}" \
  --decision-url "${DECISION_URL}" \
  --state-db "${STATE_DB}" \
  --decision-timeout-sec "${DECISION_TIMEOUT_SEC}" \
  --min-reply-interval-sec "${MIN_REPLY_INTERVAL_SEC}" \
  --fallback-text "${FALLBACK_TEXT}"
