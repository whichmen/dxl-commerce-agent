#!/usr/bin/env bash
set -u -o pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DEVICE="${MOBILE_UDID:-${1:-}}"
ADB_BIN="${ADB_BIN:-adb}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CHECK_INTERVAL_SEC="${QIANNIU_GUARD_INTERVAL_SEC:-5}"
ADB_TIMEOUT_SEC="${QIANNIU_ADB_TIMEOUT_SEC:-8}"
FORCE_START_WORKER_APP="${QIANNIU_FORCE_START_WORKER_APP:-1}"
FORCE_START_QIANNIU="${QIANNIU_FORCE_START_QIANNIU:-1}"
AUTO_UNLOCK="${QIANNIU_GUARD_AUTO_UNLOCK:-1}"
WHITE_CHECK_INTERVAL_SEC="${QIANNIU_WHITE_CHECK_INTERVAL_SEC:-300}"
case "${WHITE_CHECK_INTERVAL_SEC}" in
  ''|*[!0-9]*) WHITE_CHECK_INTERVAL_SEC="300" ;;
esac
if (( WHITE_CHECK_INTERVAL_SEC > 0 && WHITE_CHECK_INTERVAL_SEC < 300 )); then
  WHITE_CHECK_INTERVAL_SEC="300"
fi
WHITE_STREAK_TO_RECOVER="${QIANNIU_WHITE_STREAK_TO_RECOVER:-2}"
CONFIG_SYNC_INTERVAL_SEC="${QIANNIU_CONFIG_SYNC_INTERVAL_SEC:-30}"
REVERSE_SYNC_INTERVAL_SEC="${QIANNIU_REVERSE_SYNC_INTERVAL_SEC:-15}"
REVERSE_PORT="${QIANNIU_REVERSE_PORT:-18080}"
DECISION_URL="${DECISION_URL:-http://127.0.0.1:18080}"
POPUP_ASSIST_COOLDOWN_SEC="${QIANNIU_POPUP_ASSIST_COOLDOWN_SEC:-5}"
SERVICE_ATTITUDE_TAP_X="${QIANNIU_SERVICE_ATTITUDE_TAP_X:-235}"
SERVICE_ATTITUDE_TAP_Y="${QIANNIU_SERVICE_ATTITUDE_TAP_Y:-1880}"

STORE_ID="${STORE_ID:-}"
STORE_NAME="${STORE_NAME:-}"

WORKER_PKG="com.dxl.kefu.qianniu"
QIANNIU_PKG="com.taobao.qianniu"
ACCESSIBILITY_COMPONENT="com.dxl.kefu.qianniu/com.dxl.kefu.qianniu.QianNiuAccessibilityService"
WORKER_CONFIG_ACTION="com.dxl.kefu.qianniu.action.GUARD_SYNC_CONFIG"
WORKER_CONFIG_RECEIVER="com.dxl.kefu.qianniu/.GuardConfigReceiver"

if [[ -z "${DEVICE}" ]]; then
  echo "[ERR] MOBILE_UDID is required (example: emulator-5554)" >&2
  exit 2
fi

if [[ "${CHECK_INTERVAL_SEC}" == "0" ]]; then
  CHECK_INTERVAL_SEC="5"
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

latest_worker_popup_assist_line() {
  adb_run logcat -d -v time QNWkr:I "*:S" \
    | tail -n 200 \
    | grep 'GUARD_ASSIST:service_attitude' \
    | tail -n 1 \
    | tr -d '\r' || true
}

latest_worker_decide_failed_line() {
  adb_run logcat -d -v time QNWkr:I "*:S" \
    | tail -n 300 \
    | grep 'decide failed:' \
    | tail -n 1 \
    | tr -d '\r' || true
}

latest_worker_decision_ok_line() {
  adb_run logcat -d -v time QNWkr:I "*:S" \
    | tail -n 300 \
    | grep 'decision action=' \
    | grep -v 'reason=fallback:' \
    | tail -n 1 \
    | tr -d '\r' || true
}



worker_decide_in_progress() {
  local lines last_start last_done
  lines="$(adb_run logcat -d -v time QNWkr:I QNWkr:W "*:S" | tail -n 160 | tr -d '\r' || true)"
  last_start="$(printf '%s\n' "${lines}" | grep -n 'decide start' | tail -n 1 | cut -d: -f1 || true)"
  last_done="$(printf '%s\n' "${lines}" | grep -n -E 'decision action=|decide failed:|ack status=|发送失败' | tail -n 1 | cut -d: -f1 || true)"
  [[ -n "${last_start}" ]] || return 1
  [[ -z "${last_done}" ]] && return 0
  (( last_start > last_done ))
}

extract_session_key_from_line() {
  printf '%s\n' "$1" | sed -n "s/.*\\[\\([^]]*\\)\\].*/\\1/p" | head -n 1
}

send_decision_alert() {
  local action="$1"
  local detail="$2"
  local session_key="$3"
  "${PYTHON_BIN}" -u "${ROOT_DIR}/scripts/decision_alert_cli.py" "${action}" \
    --platform taobao \
    --store-id "${STORE_ID}" \
    --store-name "${STORE_NAME}" \
    --detail "${detail}" \
    --session-key "${session_key}" >/dev/null 2>&1 || true
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

handle_worker_popup_assist_once() {
  local now_epoch="$1"
  local line
  line="$(latest_worker_popup_assist_line)"
  [[ -n "${line}" ]] || return 0
  if [[ "${line}" == "${POPUP_ASSIST_LAST_LINE}" ]]; then
    return 0
  fi
  POPUP_ASSIST_LAST_LINE="${line}"

  if (( now_epoch - POPUP_ASSIST_LAST_TAP_AT < POPUP_ASSIST_COOLDOWN_SEC )); then
    log "[INFO] popup assist signal ignored by cooldown (${POPUP_ASSIST_COOLDOWN_SEC}s)"
    return 0
  fi

  if adb_shell input tap "${SERVICE_ATTITUDE_TAP_X}" "${SERVICE_ATTITUDE_TAP_Y}" >/dev/null; then
    POPUP_ASSIST_LAST_TAP_AT="${now_epoch}"
    log "[WARN] popup assist tapped once: service_attitude (${SERVICE_ATTITUDE_TAP_X},${SERVICE_ATTITUDE_TAP_Y})"
    return 0
  fi
  log "[WARN] popup assist tap failed: service_attitude"
  return 1
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

launch_pkg() {
  adb_shell monkey -p "$1" -c android.intent.category.LAUNCHER 1 >/dev/null || true
}

dump_qianniu_ui_xml() {
  adb_run shell "rm -f /sdcard/qianniu_guard_ui.xml; uiautomator dump /sdcard/qianniu_guard_ui.xml >/dev/null 2>&1; cat /sdcard/qianniu_guard_ui.xml 2>/dev/null" | tr -d '\r' || true
}

is_empty_tree_xml() {
  local xml="$1"
  [[ -n "${xml}" ]] || return 1
  [[ "${xml}" == *'package="com.taobao.qianniu"'* ]] || return 1
  local node_count
  node_count="$(printf '%s' "${xml}" | awk -F'<node ' '{print NF-1}')"
  if [[ -z "${node_count}" ]]; then
    node_count=0
  fi
  if (( node_count <= 1 )) && [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/'* ]]; then
    return 0
  fi
  return 1
}

is_overlay_only_xml() {
  local xml="$1"
  [[ -n "${xml}" ]] || return 1
  [[ "${xml}" == *'package="com.taobao.qianniu"'* ]] || return 1
  [[ "${xml}" == *'resource-id="com.taobao.qianniu:id/touch_outside"'* ]] || return 1
  [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/tv_time"'* ]] || return 1
  [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/tabLayout"'* ]] || return 1
  [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/msgcenter_panel_input_edit"'* ]] || return 1
  [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/msgflow_recycler"'* ]] || return 1
  [[ "${xml}" != *'text="接待"'* ]] || return 1
  [[ "${xml}" != *'text="重要"'* ]] || return 1
  [[ "${xml}" != *'text="交易"'* ]] || return 1
  [[ "${xml}" != *'text="店铺"'* ]] || return 1
  [[ "${xml}" != *'text="营销"'* ]] || return 1
  local node_count
  node_count="$(printf '%s' "${xml}" | awk -F'<node ' '{print NF-1}')"
  if [[ -z "${node_count}" ]]; then
    node_count=0
  fi
  if (( node_count <= 16 )); then
    return 0
  fi
  return 1
}

is_blank_chat_xml() {
  local xml="$1"
  [[ -n "${xml}" ]] || return 1
  # 形态1：历史白屏（有 chat_container 背景，但没有输入框/消息流）
  if [[ "${xml}" == *'resource-id="com.taobao.qianniu:id/chat_container"'* ]] &&
     [[ "${xml}" == *'resource-id="com.taobao.qianniu:id/iv_chat_component"'* ]] &&
     [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/msgcenter_panel_input_edit"'* ]] &&
     [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/msgflow_recycler"'* ]]; then
    return 0
  fi

  # 形态2：仅顶部栏（返回/设置）+ 无聊天区/无列表区，常见“全白+顶栏”卡死态
  if [[ "${xml}" == *'package="com.taobao.qianniu"'* ]] &&
     [[ "${xml}" == *'content-desc="返回上一页"'* ]] &&
     [[ "${xml}" == *'content-desc="设置"'* ]] &&
     [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/msgcenter_panel_input_edit"'* ]] &&
     [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/msgflow_recycler"'* ]] &&
     [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/tabLayout"'* ]] &&
     [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/tv_time"'* ]] &&
     [[ "${xml}" != *'resource-id="com.taobao.qianniu:id/slide_panel"'* ]]; then
    return 0
  fi

  return 1
}

is_broken_ui_xml() {
  local xml="$1"
  [[ -n "${xml}" ]] || return 0
  is_empty_tree_xml "${xml}" && return 0
  is_overlay_only_xml "${xml}" && return 0
  is_blank_chat_xml "${xml}" && return 0
  return 1
}

recover_from_blank_chat() {
  log "[WARN] detected qianniu blank chat, start guard recovery"
  adb_shell input keyevent KEYCODE_BACK >/dev/null || true
  sleep 1

  local xml_after_back
  xml_after_back="$(dump_qianniu_ui_xml)"
  if ! is_broken_ui_xml "${xml_after_back}"; then
    log "[INFO] blank chat recovered by BACK"
    return
  fi

  adb_shell input keyevent KEYCODE_HOME >/dev/null || true
  sleep 1
  launch_pkg "${QIANNIU_PKG}"
  sleep 2
  local xml_after_home_launch
  xml_after_home_launch="$(dump_qianniu_ui_xml)"
  if ! is_broken_ui_xml "${xml_after_home_launch}"; then
    log "[WARN] blank chat recovered by HOME+launch"
    return
  fi

  adb_shell am force-stop "${QIANNIU_PKG}" >/dev/null || true
  sleep 1
  launch_pkg "${QIANNIU_PKG}"
  sleep 2
  log "[WARN] blank chat persists after HOME+launch, force-stopped and restarted qianniu"
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

is_qianniu_foreground() {
  local focus_line app_line
  focus_line="$(current_focus_line)"
  app_line="$(focused_app_line)"
  if [[ "${focus_line}" == *"com.taobao.qianniu/"* ]]; then
    return 0
  fi
  if [[ "${app_line}" == *"com.taobao.qianniu/"* ]]; then
    return 0
  fi
  return 1
}

is_qianniu_anr_dialog() {
  local line
  line="$(current_focus_line)"
  [[ "${line}" == *"Application Not Responding: ${QIANNIU_PKG}"* ]]
}

recover_from_qianniu_anr() {
  log "[WARN] detected qianniu ANR dialog, force-stopping and restarting qianniu"
  adb_shell am force-stop "${QIANNIU_PKG}" >/dev/null || true
  sleep 1
  launch_pkg "${QIANNIU_PKG}"
  sleep 2
  if is_qianniu_foreground; then
    log "[WARN] qianniu restarted after ANR"
  else
    log "[WARN] qianniu restart after ANR did not reach foreground"
  fi
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

DEVICE_MISSING_LOGGED=0
WORKER_PKG_MISSING_LOGGED=0
QIANNIU_PKG_MISSING_LOGGED=0
WHITE_LAST_CHECK_AT=0
WHITE_STREAK=0
UI_XML_EMPTY_STREAK=0
UI_XML_EMPTY_STREAK_TO_RECOVER="${QIANNIU_UI_XML_EMPTY_STREAK_TO_RECOVER:-2}"
CONFIG_LAST_SYNC_AT=0
REVERSE_LAST_SYNC_AT=0
REVERSE_READY=0
POPUP_ASSIST_LAST_TAP_AT=0
POPUP_ASSIST_LAST_LINE=""
POPUP_ASSIST_LAST_LINE="$(latest_worker_popup_assist_line)"
DECISION_ALERT_LAST_FAIL_LINE="$(latest_worker_decide_failed_line)"
DECISION_ALERT_LAST_OK_LINE="$(latest_worker_decision_ok_line)"

log "[INFO] QianNiu accessibility guard started: udid=${DEVICE} interval=${CHECK_INTERVAL_SEC}s adb_timeout=${ADB_TIMEOUT_SEC}s"
log "[INFO] root=${ROOT_DIR} force_start_worker_app=${FORCE_START_WORKER_APP} force_start_qianniu=${FORCE_START_QIANNIU} auto_unlock=${AUTO_UNLOCK} white_check_interval=${WHITE_CHECK_INTERVAL_SEC}s white_streak=${WHITE_STREAK_TO_RECOVER} empty_xml_streak=${UI_XML_EMPTY_STREAK_TO_RECOVER} config_sync_interval=${CONFIG_SYNC_INTERVAL_SEC}s reverse_sync_interval=${REVERSE_SYNC_INTERVAL_SEC}s popup_assist_cooldown=${POPUP_ASSIST_COOLDOWN_SEC}s decision_url='${DECISION_URL}'"

while true; do
  state="$(adb_run get-state | tr -d '\r' || true)"
  if [[ "${state}" != "device" ]]; then
    if [[ "${DEVICE_MISSING_LOGGED}" == "0" ]]; then
      log "[WARN] device not ready: udid=${DEVICE}, state='${state}'"
      DEVICE_MISSING_LOGGED=1
    fi
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
    sleep "${CHECK_INTERVAL_SEC}"
    continue
  fi
  if [[ "${WORKER_PKG_MISSING_LOGGED}" == "1" ]]; then
    log "[INFO] worker package restored: ${WORKER_PKG}"
    WORKER_PKG_MISSING_LOGGED=0
  fi

  if ! pkg_exists "${QIANNIU_PKG}"; then
    if [[ "${QIANNIU_PKG_MISSING_LOGGED}" == "0" ]]; then
      log "[WARN] qianniu package missing: ${QIANNIU_PKG}"
      QIANNIU_PKG_MISSING_LOGGED=1
    fi
    sleep "${CHECK_INTERVAL_SEC}"
    continue
  fi
  if [[ "${QIANNIU_PKG_MISSING_LOGGED}" == "1" ]]; then
    log "[INFO] qianniu package restored: ${QIANNIU_PKG}"
    QIANNIU_PKG_MISSING_LOGGED=0
  fi

  ensure_accessibility

  if [[ "${AUTO_UNLOCK}" == "1" ]]; then
    if is_keyguard_showing || is_notification_shade_focus; then
      try_unlock_screen
      sleep 1
      if is_keyguard_showing; then
        log "[WARN] lockscreen still showing after unlock try"
      else
        log "[INFO] unlock applied"
      fi
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

  qianniu_pid="$(pid_of "${QIANNIU_PKG}")"
  if [[ -z "${qianniu_pid}" && "${FORCE_START_QIANNIU}" == "1" ]]; then
    launch_pkg "${QIANNIU_PKG}"
    sleep 1
    qianniu_pid="$(pid_of "${QIANNIU_PKG}")"
    if [[ -n "${qianniu_pid}" ]]; then
      log "[INFO] started qianniu app: pid=${qianniu_pid}"
    else
      log "[WARN] failed to start qianniu app"
    fi
  fi

  if [[ "${FORCE_START_QIANNIU}" == "1" && -n "${qianniu_pid}" ]]; then
    if is_qianniu_anr_dialog; then
      recover_from_qianniu_anr
      qianniu_pid="$(pid_of "${QIANNIU_PKG}")"
      WHITE_STREAK=0
      UI_XML_EMPTY_STREAK=0
      WHITE_LAST_CHECK_AT=0
    fi
    if ! is_qianniu_foreground; then
      launch_pkg "${QIANNIU_PKG}"
      sleep 1
      if is_qianniu_foreground; then
        log "[INFO] moved qianniu to foreground"
      else
        log "[WARN] qianniu still not foreground"
      fi
    fi
  fi

  now_epoch="$(date '+%s')"
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

  if [[ -n "${qianniu_pid}" ]] && is_qianniu_foreground; then
    handle_worker_popup_assist_once "${now_epoch}" || true
    if (( now_epoch - WHITE_LAST_CHECK_AT >= WHITE_CHECK_INTERVAL_SEC )); then
      WHITE_LAST_CHECK_AT="${now_epoch}"
      if worker_decide_in_progress; then
        log "[INFO] skip ui dump while worker decision is in progress"
        sleep "${CHECK_INTERVAL_SEC}"
        continue
      fi
      ui_xml="$(dump_qianniu_ui_xml)"
      if [[ -z "${ui_xml}" ]]; then
        UI_XML_EMPTY_STREAK=$((UI_XML_EMPTY_STREAK + 1))
        log "[WARN] ui dump empty ${UI_XML_EMPTY_STREAK}/${UI_XML_EMPTY_STREAK_TO_RECOVER}"
        if (( UI_XML_EMPTY_STREAK >= UI_XML_EMPTY_STREAK_TO_RECOVER )); then
          log "[WARN] ui dump empty continuously, trigger recovery"
          recover_from_blank_chat
          UI_XML_EMPTY_STREAK=0
          WHITE_STREAK=0
          WHITE_LAST_CHECK_AT=0
        fi
      elif is_overlay_only_xml "${ui_xml}"; then
        UI_XML_EMPTY_STREAK=0
        log "[WARN] overlay-only tree detected, immediate recovery"
        recover_from_blank_chat
        WHITE_STREAK=0
        WHITE_LAST_CHECK_AT=0
      elif is_empty_tree_xml "${ui_xml}"; then
        UI_XML_EMPTY_STREAK=0
        log "[WARN] hard blank tree detected, immediate recovery"
        recover_from_blank_chat
        WHITE_STREAK=0
        WHITE_LAST_CHECK_AT=0
      elif is_blank_chat_xml "${ui_xml}"; then
        UI_XML_EMPTY_STREAK=0
        WHITE_STREAK=$((WHITE_STREAK + 1))
        log "[WARN] blank chat signature hit ${WHITE_STREAK}/${WHITE_STREAK_TO_RECOVER}"
        if (( WHITE_STREAK >= WHITE_STREAK_TO_RECOVER )); then
          recover_from_blank_chat
          WHITE_STREAK=0
          WHITE_LAST_CHECK_AT=0
        fi
      else
        UI_XML_EMPTY_STREAK=0
        WHITE_STREAK=0
      fi
    fi
  else
    UI_XML_EMPTY_STREAK=0
    WHITE_STREAK=0
  fi

  if [[ -n "${worker_pid}" ]]; then
    handle_decision_alerts
  fi

  sleep "${CHECK_INTERVAL_SEC}"
done
