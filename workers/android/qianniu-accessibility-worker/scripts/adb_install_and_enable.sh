#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-}"
APK_PATH="${2:-app/build/outputs/apk/debug/app-debug.apk}"
COMPONENT="com.dxl.kefu.qianniu/com.dxl.kefu.qianniu.QianNiuAccessibilityService"

if [[ -z "$DEVICE" ]]; then
  echo "usage: $0 <udid> [apk_path]"
  exit 1
fi

if [[ ! -f "$APK_PATH" ]]; then
  echo "apk not found: $APK_PATH"
  exit 2
fi

adb -s "$DEVICE" install -r "$APK_PATH"

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

echo "[ok] installed + enabled on $DEVICE"
