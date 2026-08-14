#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-$(id -un)}"
SERVICE_HOME="${SERVICE_HOME:-$(getent passwd "$SERVICE_USER" | cut -d: -f6)}"
TEMP_UNIT="$(mktemp)"
trap 'rm -f "$TEMP_UNIT"' EXIT

sed \
  -e "s|@WORKERS_DIR@|$ROOT_DIR|g" \
  -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
  -e "s|@SERVICE_HOME@|$SERVICE_HOME|g" \
  "$ROOT_DIR/deploy/kefu-douyin-worker.service" > "$TEMP_UNIT"

sudo install -m 0644 "$TEMP_UNIT" /etc/systemd/system/kefu-douyin-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now kefu-douyin-worker.service
sudo systemctl status --no-pager kefu-douyin-worker.service
