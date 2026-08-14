#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ENV="${1:-${PROJECT_DIR}/.env}"
OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-${HOME}/.openclaw}"
DEST_ENV="${2:-${OPENCLAW_STATE_DIR}/.env}"

if [[ ! -f "${SOURCE_ENV}" ]]; then
  echo "ERROR: source env file not found: ${SOURCE_ENV}" >&2
  echo "Create it from .env.example and fill in your own values first." >&2
  exit 2
fi

managed_keys=(
  CLAWBOT_TOOL_BASE_URL
  CLAWBOT_TOOL_API_KEY
  CLAWBOT_TOOL_TIMEOUT_SEC
  CLAWBOT_ENABLE_ADMIN_QUERY_TOOLS
  CLAWBOT_ENABLE_REFUND_WRITE_TOOL
  CLAWBOT_ENABLE_BLACKLIST_WRITE_TOOLS
  CLAWBOT_ENABLE_OFFLINE_ADMIN_TOOLS
  CLAWBOT_ENABLE_IMAGE_INSPECT_TOOL
  CLAWBOT_ENABLE_FILE_READ_TOOL
  CLAWBOT_ENABLE_SHELL_EXEC_TOOL
  CLAWBOT_ENABLE_OPENCLAW_BUILTINS
  CLAWBOT_TARGET_OUTER_ID
  CLAWBOT_FILE_READ_ROOTS
  CLAWBOT_SHELL_ALLOWED_COMMANDS
)

mkdir -p "$(dirname "${DEST_ENV}")"
touch "${DEST_ENV}"
chmod 600 "${DEST_ENV}"

tmp_file="$(mktemp "${DEST_ENV}.tmp.XXXXXX")"
trap 'rm -f "${tmp_file}"' EXIT

key_regex="$(IFS='|'; printf '%s' "${managed_keys[*]}")"
awk -v keys="${key_regex}" '
  $0 !~ "^[[:space:]]*(export[[:space:]]+)?(" keys ")[[:space:]]*=" { print }
' "${DEST_ENV}" > "${tmp_file}"

{
  printf '\n# DXL Commerce Agent: managed values used by the kefu-tools plugin.\n'
  for key in "${managed_keys[@]}"; do
    line="$(awk -v wanted="${key}" '
      $0 ~ "^[[:space:]]*(export[[:space:]]+)?" wanted "[[:space:]]*=" {
        sub(/^[[:space:]]*export[[:space:]]+/, "")
        print
        exit
      }
    ' "${SOURCE_ENV}")"
    if [[ -n "${line}" ]]; then
      printf '%s\n' "${line}"
    fi
  done
} >> "${tmp_file}"

chmod 600 "${tmp_file}"
mv "${tmp_file}" "${DEST_ENV}"
trap - EXIT

echo "OK: synchronized ${#managed_keys[@]} allowed key names into ${DEST_ENV}."
echo "Values were not printed. Restart the OpenClaw Gateway to load them."
