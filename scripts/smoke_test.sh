#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${PROJECT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_DIR}/.env"
  set +a
fi

BASE_URL="${1:-http://127.0.0.1:${CLAWBOT_PORT:-18080}}"
auth_header=()
if [[ -n "${CLAWBOT_API_KEY:-}" ]]; then
  auth_header=(-H "X-Api-Key: ${CLAWBOT_API_KEY}")
fi

curl -fsS "$BASE_URL/health"
echo

curl -fsS -X POST "$BASE_URL/v1/decide" \
  "${auth_header[@]}" \
  -H 'Content-Type: application/json' \
  -d '{
    "event": {
      "tenant_id": "default",
      "platform": "douyin",
      "store_id": "douyin_store_default",
      "store_name": "test_store",
      "session_id": "session_smoke_1",
      "customer_id": "u_smoke",
      "platform_nickname": "smoke_user",
      "message_id": "m_smoke_1",
      "message_type": "text",
      "text": "发错货了",
      "media_url": "",
      "raw": {}
    }
  }'
echo

curl -fsS "${auth_header[@]}" "$BASE_URL/v1/ops/summary?minutes=120"
echo
