#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
TEMPLATE_DIR="${PROJECT_DIR}/deploy"

case "${PROJECT_DIR}" in
  *$'\n'*|*%*)
    echo "ERROR: project path cannot contain a newline or percent sign: ${PROJECT_DIR}" >&2
    exit 2
    ;;
esac

mkdir -p "${USER_UNIT_DIR}"
temp_dir="$(mktemp -d)"
trap 'rm -rf "${temp_dir}"' EXIT

escaped_project="${PROJECT_DIR//\\/\\\\}"
escaped_project="${escaped_project//&/\\&}"
escaped_project="${escaped_project//|/\\|}"

render_unit() {
  local source_file="$1"
  local unit_name="$2"
  sed "s|@PROJECT_DIR@|${escaped_project}|g" "${source_file}" > "${temp_dir}/${unit_name}"
  install -m 0644 "${temp_dir}/${unit_name}" "${USER_UNIT_DIR}/${unit_name}"
}

render_unit "${TEMPLATE_DIR}/kefu-clawbot.service" "kefu-clawbot.service"
render_unit \
  "${TEMPLATE_DIR}/openclaw-agent-bridge-kefu-ops-watchdog.service" \
  "openclaw-agent-bridge-kefu-ops-watchdog.service"
install -m 0644 \
  "${TEMPLATE_DIR}/openclaw-agent-bridge-kefu-ops-watchdog.timer" \
  "${USER_UNIT_DIR}/openclaw-agent-bridge-kefu-ops-watchdog.timer"

if [[ "${KEFU_INSTALL_NO_START:-0}" == "1" ]]; then
  echo "OK: rendered user units into ${USER_UNIT_DIR}; start was skipped."
  exit 0
fi

systemctl --user daemon-reload
systemctl --user enable --now kefu-clawbot.service
systemctl --user enable --now openclaw-agent-bridge-kefu-ops-watchdog.timer

echo "OK: installed user services from ${PROJECT_DIR}."
echo "Check them with:"
echo "  systemctl --user status kefu-clawbot.service"
echo "  systemctl --user status openclaw-agent-bridge-kefu-ops-watchdog.timer"
