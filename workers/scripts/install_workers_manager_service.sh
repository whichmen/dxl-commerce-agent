#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_HOME="${SERVICE_HOME:-$(getent passwd "$SERVICE_USER" | cut -d: -f6)}"

if [[ -z "$SERVICE_HOME" ]]; then
  echo "ERROR: cannot resolve home directory for $SERVICE_USER" >&2
  exit 1
fi

render_unit() {
  local source_file="$1"
  local unit_name="$2"
  local temp_file
  temp_file="$(mktemp)"
  sed \
    -e "s|@WORKERS_DIR@|$ROOT_DIR|g" \
    -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
    -e "s|@SERVICE_HOME@|$SERVICE_HOME|g" \
    "$source_file" > "$temp_file"
  sudo install -m 0644 "$temp_file" "/etc/systemd/system/$unit_name"
  rm -f "$temp_file"
}

render_unit "$ROOT_DIR/deploy/kefu-workers-manager.service" kefu-workers-manager.service
render_unit "$ROOT_DIR/deploy/kefu-workers-manager.path" kefu-workers-manager.path
render_unit "$ROOT_DIR/deploy/kefu-workers-manager-reload.service" kefu-workers-manager-reload.service
render_unit "$ROOT_DIR/deploy/kefu-worker-browser-reload.service" kefu-worker-browser-reload.service
render_unit "$ROOT_DIR/deploy/kefu-worker-browser-reload.timer" kefu-worker-browser-reload.timer

sudo systemctl daemon-reload
sudo systemctl disable --now kefu-douyin-worker.service 2>/dev/null || true
sudo systemctl enable --now kefu-workers-manager.service
sudo systemctl enable --now kefu-workers-manager.path
sudo systemctl enable --now kefu-worker-browser-reload.timer
sudo systemctl status --no-pager kefu-workers-manager.service | sed -n '1,40p'
