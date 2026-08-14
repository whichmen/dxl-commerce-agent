#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-}"
COMPONENT="com.dxl.kefu.qianniu/com.dxl.kefu.qianniu.QianNiuAccessibilityService"

if [[ -z "$DEVICE" ]]; then
  echo "usage: $0 <udid>"
  exit 1
fi

CUR="$(adb -s "$DEVICE" shell settings get secure enabled_accessibility_services | tr -d '\r')"
if [[ "$CUR" == "null" || -z "$CUR" ]]; then
  NEW="$COMPONENT"
elif [[ ":$CUR:" == *":$COMPONENT:"* ]]; then
  NEW="$CUR"
else
  NEW="$CUR:$COMPONENT"
fi

adb -s "$DEVICE" shell settings put secure enabled_accessibility_services "$NEW"
adb -s "$DEVICE" shell settings put secure accessibility_enabled 1

echo "[ok] enabled on $DEVICE"
adb -s "$DEVICE" shell settings get secure accessibility_enabled
echo "---"
adb -s "$DEVICE" shell settings get secure enabled_accessibility_services
