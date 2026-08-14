#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python3}"
REMOTE_PORT="${REMOTE_PORT:-9300}"
STORE_ID="${STORE_ID:-browser_store_example}"
STORE_NAME="${STORE_NAME:-示例浏览器店铺}"
STATE_DB="${STATE_DB:-${ROOT_DIR}/state/browser_keepalive.db}"
TARGET_URL="${TARGET_URL:-https://ark.xiaohongshu.com/}"
CHROME_LOG="${CHROME_LOG:-/tmp/chrome_browser_${REMOTE_PORT}.log}"
CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-${ROOT_DIR}/chrome_user_data/browser_example}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-Default}"
BROWSER_KEEPALIVE_INTERVAL_SEC="${BROWSER_KEEPALIVE_INTERVAL_SEC:-15}"

mkdir -p "$(dirname "${STATE_DB}")"

export PYTHON_BIN REMOTE_PORT STORE_ID STORE_NAME STATE_DB
export TARGET_URL CHROME_LOG CHROME_USER_DATA_DIR CHROME_PROFILE_DIR
export BROWSER_KEEPALIVE_INTERVAL_SEC

"${ROOT_DIR}/scripts/start_chrome_9222.sh"

exec "${PYTHON_BIN}" -u "${ROOT_DIR}/scripts/browser_keepalive_worker.py"
