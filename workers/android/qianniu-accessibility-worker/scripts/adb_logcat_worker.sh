#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-}"
if [[ -z "$DEVICE" ]]; then
  echo "usage: $0 <udid>"
  exit 1
fi

adb -s "$DEVICE" logcat -v time QNWkr:I *:S
