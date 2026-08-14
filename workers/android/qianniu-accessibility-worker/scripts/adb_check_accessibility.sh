#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-}"
if [[ -z "$DEVICE" ]]; then
  echo "usage: $0 <udid>"
  exit 1
fi

echo "[device] $DEVICE"
adb -s "$DEVICE" shell settings get secure accessibility_enabled || true
echo "---"
adb -s "$DEVICE" shell settings get secure enabled_accessibility_services || true
