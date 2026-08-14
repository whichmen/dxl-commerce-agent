#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DEVICE="${MOBILE_UDID:-${1:-}}"
ADB_BIN="${ADB_BIN:-adb}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
HEALTH_CLI="${ROOT_DIR}/scripts/worker_health_cli.py"
CHECK_INTERVAL_SEC="${QIANFAN_GUARD_INTERVAL_SEC:-5}"
HEALTH_CHECK_INTERVAL_SEC="${QIANFAN_HEALTH_CHECK_INTERVAL_SEC:-15}"
HEALTH_UI_DUMP_ENABLED="${QIANFAN_HEALTH_UI_DUMP_ENABLED:-0}"
ADB_TIMEOUT_SEC="${QIANFAN_ADB_TIMEOUT_SEC:-8}"
FORCE_START_WORKER_APP="${QIANFAN_FORCE_START_WORKER_APP:-1}"
FORCE_START_QIANFAN="${QIANFAN_FORCE_START_QIANFAN:-1}"
AUTO_UNLOCK="${QIANFAN_GUARD_AUTO_UNLOCK:-1}"
CONFIG_SYNC_INTERVAL_SEC="${QIANFAN_CONFIG_SYNC_INTERVAL_SEC:-30}"
REVERSE_SYNC_INTERVAL_SEC="${QIANFAN_REVERSE_SYNC_INTERVAL_SEC:-15}"
REVERSE_PORT="${QIANFAN_REVERSE_PORT:-18080}"
DECISION_URL="${DECISION_URL:-http://127.0.0.1:18080}"
ORDER_PAGE_BACK_COOLDOWN_SEC="${QIANFAN_ORDER_PAGE_BACK_COOLDOWN_SEC:-3}"
FULLSCREEN_PREVIEW_BACK_AFTER_SEC="${QIANFAN_FULLSCREEN_PREVIEW_BACK_AFTER_SEC:-30}"
FULLSCREEN_PREVIEW_BACK_COOLDOWN_SEC="${QIANFAN_FULLSCREEN_PREVIEW_BACK_COOLDOWN_SEC:-60}"

STORE_ID="${STORE_ID:-}"
STORE_NAME="${STORE_NAME:-}"

WORKER_PKG="com.dxl.kefu.qianfan"
QIANFAN_PKG="com.xingin.eva"
ACCESSIBILITY_COMPONENT="com.dxl.kefu.qianfan/com.dxl.kefu.qianfan.QianFanAccessibilityService"
WORKER_CONFIG_ACTION="com.dxl.kefu.qianfan.action.GUARD_SYNC_CONFIG"
WORKER_CONFIG_RECEIVER="com.dxl.kefu.qianfan/.GuardConfigReceiver"

if [[ -z "${DEVICE}" ]]; then
  echo "[ERR] MOBILE_UDID is required (example: emulator-5554)" >&2
  exit 2
fi

if ! command -v "${ADB_BIN}" >/dev/null 2>&1; then
  echo "[ERR] adb not found: ${ADB_BIN}" >&2
  exit 2
fi

HAS_TIMEOUT=0
if command -v timeout >/dev/null 2>&1; then
  HAS_TIMEOUT=1
fi

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

adb_run() {
  if [[ "${HAS_TIMEOUT}" == "1" ]]; then
    timeout "${ADB_TIMEOUT_SEC}s" "${ADB_BIN}" -s "${DEVICE}" "$@" 2>/dev/null
  else
    "${ADB_BIN}" -s "${DEVICE}" "$@" 2>/dev/null
  fi
}

adb_shell() {
  adb_run shell "$@"
}

get_setting() {
  adb_shell settings get secure "$1" | tr -d '\r' || true
}

put_setting() {
  adb_shell settings put secure "$1" "$2" >/dev/null || true
}

pid_of() {
  adb_shell pidof "$1" | tr -d '\r' || true
}

pkg_exists() {
  local out
  out="$(adb_shell pm path "$1" | tr -d '\r' || true)"
  [[ "${out}" == package:* ]]
}

window_dump() {
  adb_shell dumpsys window 2>/dev/null || true
}

current_focus_line() {
  window_dump | grep -m1 "mCurrentFocus=" || true
}

focused_app_line() {
  window_dump | grep -m1 "mFocusedApp=" || true
}

is_qianfan_webview_foreground() {
  local focus_line app_line
  focus_line="$(current_focus_line)"
  app_line="$(focused_app_line)"
  [[ "${focus_line}" == *"com.xingin.eva/com.xingin.xywebview.activity.WebViewActivityV2"* ||
     "${app_line}" == *"com.xingin.eva/com.xingin.xywebview.activity.WebViewActivityV2"* ]]
}

is_keyguard_showing() {
  local dump
  dump="$(window_dump)"
  [[ "${dump}" == *"isKeyguardShowing=true"* ]]
}

is_notification_shade_focus() {
  local line
  line="$(current_focus_line)"
  [[ "${line}" == *"NotificationShade"* ]]
}

is_qianfan_foreground() {
  local focus_line app_line
  focus_line="$(current_focus_line)"
  app_line="$(focused_app_line)"
  [[ "${focus_line}" == *"com.xingin.eva/"* || "${app_line}" == *"com.xingin.eva/"* ]]
}

launch_pkg() {
  adb_shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null || true
}

try_unlock_screen() {
  adb_shell input keyevent KEYCODE_WAKEUP >/dev/null || true
  adb_shell wm dismiss-keyguard >/dev/null || true
  adb_shell input swipe 540 1820 540 420 220 >/dev/null || true
  adb_shell input keyevent 82 >/dev/null || true
}

ensure_accessibility() {
  local cur_enabled cur_services new_services need_fix
  cur_enabled="$(get_setting accessibility_enabled)"
  cur_services="$(get_setting enabled_accessibility_services)"
  need_fix=0

  if [[ "${cur_enabled}" != "1" ]]; then
    need_fix=1
  fi
  if [[ ":${cur_services}:" != *":${ACCESSIBILITY_COMPONENT}:"* ]]; then
    need_fix=1
  fi

  if [[ "${need_fix}" == "0" ]]; then
    return 0
  fi

  if [[ -z "${cur_services}" || "${cur_services}" == "null" ]]; then
    new_services="${ACCESSIBILITY_COMPONENT}"
  elif [[ ":${cur_services}:" == *":${ACCESSIBILITY_COMPONENT}:"* ]]; then
    new_services="${cur_services}"
  else
    new_services="${cur_services}:${ACCESSIBILITY_COMPONENT}"
  fi

  put_setting enabled_accessibility_services "${new_services}"
  put_setting accessibility_enabled "1"
  log "[INFO] repaired accessibility settings"
}

sync_worker_config() {
  [[ -n "${STORE_ID}" ]] || return 1
  local out
  out="$(
    adb_shell am broadcast \
      -a "${WORKER_CONFIG_ACTION}" \
      -n "${WORKER_CONFIG_RECEIVER}" \
      --es store_id "${STORE_ID}" \
      --es store_name "${STORE_NAME}" \
      --es device_serial "${DEVICE}" \
      --es decision_url "${DECISION_URL}" \
      | tr -d '\r' || true
  )"
  [[ "${out}" == *"Broadcast completed"* ]]
}

latest_worker_decide_failed_line() {
  adb_run logcat -d -v time QFWkr:I "*:S" \
    | tail -n 300 \
    | grep 'decide failed:' \
    | tail -n 1 \
    | tr -d '\r' || true
}

latest_worker_decision_ok_line() {
  adb_run logcat -d -v time QFWkr:I "*:S" \
    | tail -n 300 \
    | grep -E 'decision action=|已发送回复' \
    | grep -v 'reason=fallback:' \
    | tail -n 1 \
    | tr -d '\r' || true
}

extract_session_key_from_line() {
  printf '%s\n' "$1" | sed -n "s/.*\\[\\([^]]*\\)\\].*/\\1/p" | head -n 1
}

send_decision_alert() {
  local action="$1"
  local detail="$2"
  local session_key="$3"
  "${PYTHON_BIN}" -u "${ROOT_DIR}/scripts/decision_alert_cli.py" "${action}" \
    --platform xiaohongshu \
    --store-id "${STORE_ID}" \
    --store-name "${STORE_NAME}" \
    --detail "${detail}" \
    --session-key "${session_key}" >/dev/null 2>&1 || true
}

report_health() {
  local action="$1"
  local detail="${2:-}"
  "${PYTHON_BIN}" -u "${HEALTH_CLI}" "${action}" "${detail}" >/dev/null 2>&1 || true
}

dump_qianfan_ui_xml() {
  adb_run shell "rm -f /sdcard/qianfan_guard_ui.xml; uiautomator dump /sdcard/qianfan_guard_ui.xml >/dev/null 2>&1; cat /sdcard/qianfan_guard_ui.xml 2>/dev/null" | tr -d '\r' || true
}

qianfan_manual_action_detail() {
  local xml="$1"
  local focus_line="$2"
  local app_line="$3"
  if [[ "${focus_line}" == *"com.xingin.eva/com.xingin.login.activity.LoginActivity"* ||
        "${app_line}" == *"com.xingin.eva/com.xingin.login.activity.LoginActivity"* ]]; then
    printf '%s\n' "小红书千帆显示登录页，需要重新登录/输入验证码"
    return 0
  fi
  if [[ "${xml}" == *"手机号登录"* && "${xml}" == *"输入验证码"* ]]; then
    printf '%s\n' "小红书千帆显示登录页，需要重新登录/输入验证码"
    return 0
  fi
  if [[ "${xml}" == *"获取验证码"* ]]; then
    printf '%s\n' "小红书千帆显示登录页，需要重新登录/输入验证码"
    return 0
  fi
  if [[ "${xml}" == *"【离线】状态下无法接收买家消息"* ]]; then
    printf '%s\n' "小红书千帆客服离线，需要切换在线"
    return 0
  fi
  if [[ "${xml}" == *"离线"* && "${xml}" == *"切换在线"* ]]; then
    printf '%s\n' "小红书千帆客服离线，需要切换在线"
    return 0
  fi
  return 1
}


qianfan_xml_has_large_image() {
  local xml="$1"
  local node left top right bottom width height
  while IFS= read -r node; do
    if [[ "${node}" == *'class="android.widget.ImageView"'* &&
          "${node}" =~ bounds=\"\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]\" ]]; then
      left="${BASH_REMATCH[1]}"
      top="${BASH_REMATCH[2]}"
      right="${BASH_REMATCH[3]}"
      bottom="${BASH_REMATCH[4]}"
      width=$((right - left))
      height=$((bottom - top))
      if (( width >= 480 && height >= 900 )); then
        return 0
      fi
    fi
  done < <(printf '%s\n' "${xml}" | sed 's/></>\n</g')
  return 1
}

qianfan_xml_looks_fullscreen_image_preview() {
  local xml="$1"
  [[ -n "${xml}" ]] || return 1
  [[ "${xml}" == *'package="com.xingin.eva"'* ]] || return 1
  [[ "${xml}" == *'class="android.widget.EditText"'* ]] && return 1
  [[ "${xml}" == *'text="客服接待"'* ]] && return 1
  [[ "${xml}" == *'text="当前会话"'* ]] && return 1
  [[ "${xml}" == *'text="全部会话"'* ]] && return 1
  [[ "${xml}" == *'text="收藏会话"'* ]] && return 1
  [[ "${xml}" == *'text="发送"'* ]] && return 1
  [[ "${xml}" == *'text="消息"'* ]] && return 1
  [[ "${xml}" =~ text=\"[0-9]{1,2}/[0-9]{1,2}\" ]] || return 1
  qianfan_xml_has_large_image "${xml}"
}

handle_fullscreen_preview_guard() {
  local xml="$1"
  local now elapsed
  if ! qianfan_xml_looks_fullscreen_image_preview "${xml}"; then
    FULLSCREEN_PREVIEW_FIRST_SEEN_AT=0
    return 1
  fi

  now="$(date '+%s')"
  if (( FULLSCREEN_PREVIEW_FIRST_SEEN_AT <= 0 )); then
    FULLSCREEN_PREVIEW_FIRST_SEEN_AT="${now}"
    log "[WARN] detected qianfan fullscreen image preview, waiting before auto exit"
    return 0
  fi

  elapsed=$((now - FULLSCREEN_PREVIEW_FIRST_SEEN_AT))
  if (( elapsed >= FULLSCREEN_PREVIEW_BACK_AFTER_SEC &&
        now - FULLSCREEN_PREVIEW_LAST_BACK_AT >= FULLSCREEN_PREVIEW_BACK_COOLDOWN_SEC )); then
    FULLSCREEN_PREVIEW_LAST_BACK_AT="${now}"
    FULLSCREEN_PREVIEW_FIRST_SEEN_AT=0
    adb_shell input tap 540 1060 >/dev/null || true
    sleep 1
    log "[WARN] qianfan fullscreen image preview stuck ${elapsed}s, sent center tap"
  fi
  return 0
}

update_qianfan_health() {
  local focus_line app_line xml detail
  focus_line="$(current_focus_line)"
  app_line="$(focused_app_line)"
  xml=""
  if [[ "${HEALTH_UI_DUMP_ENABLED}" == "1" &&
        ( "${focus_line}" == *"com.xingin.eva/"* || "${app_line}" == *"com.xingin.eva/"* ) ]]; then
    xml="$(dump_qianfan_ui_xml)"
  fi
  if [[ "${HEALTH_UI_DUMP_ENABLED}" == "1" ]] && handle_fullscreen_preview_guard "${xml}"; then
    report_health "manual_action" "小红书千帆疑似卡在图片预览，自动恢复中"
    return 0
  fi
  if detail="$(qianfan_manual_action_detail "${xml}" "${focus_line}" "${app_line}")"; then
    report_health "manual_action" "${detail}"
    return 0
  fi
  if [[ "${focus_line}" == *"com.xingin.eva/"* || "${app_line}" == *"com.xingin.eva/"* ]]; then
    report_health "ready" "qianfan_monitoring"
  else
    report_health "manual_action" "小红书千帆未在前台"
  fi
}

handle_decision_alerts() {
  local fail_line ok_line fail_session ok_session
  fail_line="$(latest_worker_decide_failed_line)"
  if [[ -n "${fail_line}" && "${fail_line}" != "${DECISION_ALERT_LAST_FAIL_LINE}" ]]; then
    DECISION_ALERT_LAST_FAIL_LINE="${fail_line}"
    fail_session="$(extract_session_key_from_line "${fail_line}")"
    send_decision_alert "failure" "${fail_line}" "${fail_session}"
  fi

  ok_line="$(latest_worker_decision_ok_line)"
  if [[ -n "${ok_line}" && "${ok_line}" != "${DECISION_ALERT_LAST_OK_LINE}" ]]; then
    DECISION_ALERT_LAST_OK_LINE="${ok_line}"
    ok_session="$(extract_session_key_from_line "${ok_line}")"
    send_decision_alert "recovery" "${ok_line}" "${ok_session}"
  fi
}

ensure_adb_reverse() {
  local list
  list="$(adb_run reverse --list | tr -d '\r' || true)"
  if [[ "${list}" == *"tcp:${REVERSE_PORT} tcp:${REVERSE_PORT}"* ]]; then
    return 0
  fi
  adb_run reverse "tcp:${REVERSE_PORT}" "tcp:${REVERSE_PORT}" >/dev/null || return 1
  return 0
}

DEVICE_MISSING_LOGGED=0
WORKER_PKG_MISSING_LOGGED=0
QIANFAN_PKG_MISSING_LOGGED=0
CONFIG_LAST_SYNC_AT=0
REVERSE_LAST_SYNC_AT=0
REVERSE_READY=0
ORDER_PAGE_LAST_BACK_AT=0
FULLSCREEN_PREVIEW_FIRST_SEEN_AT=0
FULLSCREEN_PREVIEW_LAST_BACK_AT=0
HEALTH_LAST_CHECK_AT=0
DECISION_ALERT_LAST_FAIL_LINE="$(latest_worker_decide_failed_line)"
DECISION_ALERT_LAST_OK_LINE="$(latest_worker_decision_ok_line)"

log "[INFO] QianFan accessibility guard started: udid=${DEVICE} interval=${CHECK_INTERVAL_SEC}s adb_timeout=${ADB_TIMEOUT_SEC}s"
log "[INFO] root=${ROOT_DIR} force_start_worker_app=${FORCE_START_WORKER_APP} force_start_qianfan=${FORCE_START_QIANFAN} auto_unlock=${AUTO_UNLOCK} config_sync_interval=${CONFIG_SYNC_INTERVAL_SEC}s reverse_sync_interval=${REVERSE_SYNC_INTERVAL_SEC}s health_ui_dump=${HEALTH_UI_DUMP_ENABLED} decision_url='${DECISION_URL}'"
report_health "start" "qianfan_guard_starting"

while true; do
  state="$(adb_run get-state | tr -d '\r' || true)"
  if [[ "${state}" != "device" ]]; then
    if [[ "${DEVICE_MISSING_LOGGED}" == "0" ]]; then
      log "[WARN] device not ready: udid=${DEVICE}, state='${state}'"
      DEVICE_MISSING_LOGGED=1
    fi
    report_health "manual_action" "设备未连接: ${state:-unknown}"
    sleep "${CHECK_INTERVAL_SEC}"
    continue
  fi

  if [[ "${DEVICE_MISSING_LOGGED}" == "1" ]]; then
    log "[INFO] device online: udid=${DEVICE}"
    DEVICE_MISSING_LOGGED=0
  fi

  if ! pkg_exists "${WORKER_PKG}"; then
    if [[ "${WORKER_PKG_MISSING_LOGGED}" == "0" ]]; then
      log "[WARN] worker package missing: ${WORKER_PKG}"
      WORKER_PKG_MISSING_LOGGED=1
    fi
    report_health "manual_action" "千帆辅助包缺失: ${WORKER_PKG}"
    sleep "${CHECK_INTERVAL_SEC}"
    continue
  fi
  if [[ "${WORKER_PKG_MISSING_LOGGED}" == "1" ]]; then
    log "[INFO] worker package restored: ${WORKER_PKG}"
    WORKER_PKG_MISSING_LOGGED=0
  fi

  if ! pkg_exists "${QIANFAN_PKG}"; then
    if [[ "${QIANFAN_PKG_MISSING_LOGGED}" == "0" ]]; then
      log "[WARN] qianfan package missing: ${QIANFAN_PKG}"
      QIANFAN_PKG_MISSING_LOGGED=1
    fi
    report_health "manual_action" "小红书千帆未安装: ${QIANFAN_PKG}"
    sleep "${CHECK_INTERVAL_SEC}"
    continue
  fi
  if [[ "${QIANFAN_PKG_MISSING_LOGGED}" == "1" ]]; then
    log "[INFO] qianfan package restored: ${QIANFAN_PKG}"
    QIANFAN_PKG_MISSING_LOGGED=0
  fi

  ensure_accessibility

  if [[ "${AUTO_UNLOCK}" == "1" ]]; then
    if is_keyguard_showing || is_notification_shade_focus; then
      try_unlock_screen
      sleep 1
    fi
  fi

  worker_pid="$(pid_of "${WORKER_PKG}")"
  if [[ -z "${worker_pid}" && "${FORCE_START_WORKER_APP}" == "1" ]]; then
    launch_pkg "${WORKER_PKG}"
    sleep 1
    worker_pid="$(pid_of "${WORKER_PKG}")"
    if [[ -n "${worker_pid}" ]]; then
      log "[INFO] started worker app: pid=${worker_pid}"
    else
      log "[WARN] failed to start worker app"
    fi
  fi

  qianfan_pid="$(pid_of "${QIANFAN_PKG}")"
  if [[ -z "${qianfan_pid}" && "${FORCE_START_QIANFAN}" == "1" ]]; then
    launch_pkg "${QIANFAN_PKG}"
    sleep 1
    qianfan_pid="$(pid_of "${QIANFAN_PKG}")"
    if [[ -n "${qianfan_pid}" ]]; then
      log "[INFO] started qianfan app: pid=${qianfan_pid}"
    else
      log "[WARN] failed to start qianfan app"
    fi
  fi

  if [[ "${FORCE_START_QIANFAN}" == "1" && -n "${qianfan_pid}" ]]; then
    if ! is_qianfan_foreground; then
      launch_pkg "${QIANFAN_PKG}"
      sleep 1
      if is_qianfan_foreground; then
        log "[INFO] moved qianfan to foreground"
      else
        log "[WARN] qianfan still not foreground"
      fi
    fi
  fi

  now_epoch="$(date '+%s')"
  if is_qianfan_webview_foreground; then
    if (( now_epoch - ORDER_PAGE_LAST_BACK_AT >= ORDER_PAGE_BACK_COOLDOWN_SEC )); then
      ORDER_PAGE_LAST_BACK_AT="${now_epoch}"
      adb_shell input keyevent KEYCODE_BACK >/dev/null || true
      sleep 1
      log "[WARN] detected qianfan webview page, sent BACK to recover customer list path"
    fi
  fi

  if (( now_epoch - REVERSE_LAST_SYNC_AT >= REVERSE_SYNC_INTERVAL_SEC )); then
    REVERSE_LAST_SYNC_AT="${now_epoch}"
    if ensure_adb_reverse; then
      if [[ "${REVERSE_READY}" == "0" ]]; then
        log "[INFO] ensured adb reverse: tcp:${REVERSE_PORT}->tcp:${REVERSE_PORT}"
      fi
      REVERSE_READY=1
    else
      if [[ "${REVERSE_READY}" == "1" ]]; then
        log "[WARN] failed to ensure adb reverse: tcp:${REVERSE_PORT}->tcp:${REVERSE_PORT}"
      fi
      REVERSE_READY=0
    fi
  fi

  if [[ -n "${worker_pid}" ]] && (( now_epoch - CONFIG_LAST_SYNC_AT >= CONFIG_SYNC_INTERVAL_SEC )); then
    CONFIG_LAST_SYNC_AT="${now_epoch}"
    if sync_worker_config; then
      log "[INFO] synced worker config: store_id='${STORE_ID}' device_serial='${DEVICE}' decision_url='${DECISION_URL}'"
    else
      log "[WARN] failed to sync worker config: store_id='${STORE_ID}' device_serial='${DEVICE}' decision_url='${DECISION_URL}'"
    fi
  fi

  if [[ -n "${worker_pid}" ]]; then
    handle_decision_alerts
  fi

  if (( now_epoch - HEALTH_LAST_CHECK_AT >= HEALTH_CHECK_INTERVAL_SEC )); then
    HEALTH_LAST_CHECK_AT="${now_epoch}"
    update_qianfan_health
  fi

  sleep "${CHECK_INTERVAL_SEC}"
done
