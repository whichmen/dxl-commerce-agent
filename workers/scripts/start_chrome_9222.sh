#!/usr/bin/env bash
set -euo pipefail

REMOTE_PORT="${REMOTE_PORT:-9222}"
CHROME_BIN="${CHROME_BIN:-google-chrome}"
CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-$HOME/.chrome-kefu-douyin}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-Default}"
TARGET_URL="${TARGET_URL:-https://im.jinritemai.com/pc_seller_v2/main/workspace}"
CHROME_LOG="${CHROME_LOG:-/tmp/chrome_douyin_9222.log}"
WORKER_OPERATOR="${WORKER_OPERATOR:-}"
STORE_ID="${STORE_ID:-store_default}"
STORE_NAME="${STORE_NAME:-未命名店铺}"

mkdir -p "${CHROME_USER_DATA_DIR}"

cdp_ok() {
  curl -fsS "http://127.0.0.1:${REMOTE_PORT}/json/version" >/dev/null 2>&1
}

tab_has_target() {
  TARGET_URL="${TARGET_URL}" python3 -c '
import json
import os
import sys
from urllib.parse import urlsplit

target = os.environ.get("TARGET_URL", "").strip()
if not target:
    sys.exit(1)

parsed = urlsplit(target)
origin_prefix = ""
if parsed.scheme and parsed.netloc:
    origin_prefix = f"{parsed.scheme}://{parsed.netloc}/"

try:
    tabs = json.load(sys.stdin)
except Exception:
    sys.exit(1)

for tab in tabs:
    url = str(tab.get("url") or "").strip()
    if not url:
        continue
    if url.startswith(target):
        sys.exit(0)
    if origin_prefix and url.startswith(origin_prefix):
        sys.exit(0)

sys.exit(1)
' < <(curl -fsS "http://127.0.0.1:${REMOTE_PORT}/json/list" 2>/dev/null)
}

close_tab_by_id() {
  local tab_id="$1"
  curl -fsS "http://127.0.0.1:${REMOTE_PORT}/json/close/${tab_id}" >/dev/null 2>&1 || true
}

close_legacy_hint_tabs() {
  while IFS= read -r tab_id; do
    [[ -n "${tab_id}" ]] || continue
    close_tab_by_id "${tab_id}"
  done < <(
    curl -fsS "http://127.0.0.1:${REMOTE_PORT}/json/list" 2>/dev/null | python3 -c '
import json
import sys

try:
    tabs = json.load(sys.stdin)
except Exception:
    sys.exit(0)

for tab in tabs:
    url = str(tab.get("url") or "")
    title = str(tab.get("title") or "")
    tab_id = str(tab.get("id") or "")
    if not tab_id:
        continue
    if title.startswith("登录指引 | "):
        print(tab_id)
        continue
    if url.startswith("data:text/html") and "登录指引" in title:
        print(tab_id)
        continue
    if url.startswith("file://") and url.endswith("/worker_login_hint.html"):
        print(tab_id)
'
  )
}

open_url_tab() {
  local url_to_open="$1"
  local encoded_url
  encoded_url="$(
    URL_TO_OPEN="${url_to_open}" python3 -c 'import os, urllib.parse; print(urllib.parse.quote(os.environ["URL_TO_OPEN"], safe=":/?&=%#,"))'
  )"
  curl -fsS -X PUT "http://127.0.0.1:${REMOTE_PORT}/json/new?${encoded_url}" >/dev/null 2>&1 || true
}

ensure_login_bookmark() {
  local profile_root
  profile_root="${CHROME_USER_DATA_DIR}/${CHROME_PROFILE_DIR}"

  PROFILE_ROOT="${profile_root}" \
  WORKER_OPERATOR="${WORKER_OPERATOR}" \
  STORE_ID="${STORE_ID}" \
  STORE_NAME="${STORE_NAME}" \
  TARGET_URL="${TARGET_URL}" \
  python3 - <<'PY'
import json
import time
import uuid
from pathlib import Path
import os


def chrome_ts() -> str:
    return str(int((time.time() + 11644473600) * 1000000))


def make_folder(folder_id: str, guid: str, name: str) -> dict:
    return {
        "children": [],
        "date_added": chrome_ts(),
        "date_last_used": "0",
        "date_modified": "0",
        "guid": guid,
        "id": folder_id,
        "name": name,
        "type": "folder",
    }


def default_bookmarks() -> dict:
    return {
        "checksum": "",
        "roots": {
            "bookmark_bar": make_folder("1", "0bc5d13f-2cba-5d74-951f-3f233fe6c908", "书签栏"),
            "other": make_folder("2", "82b081ec-3dd3-529c-8475-ab6c344590dd", "其他书签"),
            "synced": make_folder("3", "4cf2e351-0e85-532b-bb37-df045d8f8d0f", "移动设备书签"),
        },
        "sync_metadata": "",
        "version": 1,
    }


def walk_max_id(node: object) -> int:
    if isinstance(node, dict):
        current = 0
        raw_id = str(node.get("id") or "").strip()
        if raw_id.isdigit():
            current = int(raw_id)
        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                current = max(current, walk_max_id(child))
        return current
    if isinstance(node, list):
        current = 0
        for child in node:
            current = max(current, walk_max_id(child))
        return current
    return 0


profile_root = Path(os.environ["PROFILE_ROOT"]).expanduser()
bookmarks_path = profile_root / "Bookmarks"
prefs_path = profile_root / "Preferences"
profile_root.mkdir(parents=True, exist_ok=True)

operator = (os.environ.get("WORKER_OPERATOR") or "").strip()
store_id = (os.environ.get("STORE_ID") or "").strip()
store_name = (os.environ.get("STORE_NAME") or "").strip()
target_url = (os.environ.get("TARGET_URL") or "").strip()

title_parts = [part for part in (operator, store_name) if part]
bookmark_title = " | ".join(title_parts) if title_parts else (store_id or "客服登录")
bookmark_url = target_url or "about:blank"

if bookmarks_path.exists():
    try:
        bookmarks = json.loads(bookmarks_path.read_text(encoding="utf-8"))
    except Exception:
        bookmarks = default_bookmarks()
else:
    bookmarks = default_bookmarks()

roots = bookmarks.setdefault("roots", {})
bookmark_bar = roots.setdefault("bookmark_bar", make_folder("1", "0bc5d13f-2cba-5d74-951f-3f233fe6c908", "书签栏"))
roots.setdefault("other", make_folder("2", "82b081ec-3dd3-529c-8475-ab6c344590dd", "其他书签"))
roots.setdefault("synced", make_folder("3", "4cf2e351-0e85-532b-bb37-df045d8f8d0f", "移动设备书签"))

children = bookmark_bar.get("children")
if not isinstance(children, list):
    children = []

marker_key = "kefu_worker_login_bookmark"
filtered_children = []
for child in children:
    if not isinstance(child, dict):
        filtered_children.append(child)
        continue
    meta = child.get("meta_info")
    if isinstance(meta, dict) and meta.get(marker_key) == "1":
        continue
    filtered_children.append(child)

next_id = max(3, walk_max_id(bookmarks)) + 1
now = chrome_ts()
filtered_children.insert(0, {
    "date_added": now,
    "date_last_used": "0",
    "guid": str(uuid.uuid4()),
    "id": str(next_id),
    "meta_info": {
        marker_key: "1",
        "power_bookmark_meta": "",
    },
    "name": bookmark_title,
    "type": "url",
    "url": bookmark_url,
})

bookmark_bar["children"] = filtered_children
bookmark_bar["date_modified"] = now
bookmarks["version"] = 1
bookmarks.setdefault("checksum", "")
bookmarks.setdefault("sync_metadata", "")
bookmarks_path.write_text(json.dumps(bookmarks, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

prefs = {}
if prefs_path.exists():
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except Exception:
        prefs = {}

bookmark_bar_pref = prefs.setdefault("bookmark_bar", {})
if not isinstance(bookmark_bar_pref, dict):
    bookmark_bar_pref = {}
    prefs["bookmark_bar"] = bookmark_bar_pref
bookmark_bar_pref["show_on_all_tabs"] = True
bookmark_bar_pref["show_apps_shortcut"] = False

account_values = prefs.setdefault("account_values", {})
if not isinstance(account_values, dict):
    account_values = {}
    prefs["account_values"] = account_values
account_bookmark_bar = account_values.setdefault("bookmark_bar", {})
if not isinstance(account_bookmark_bar, dict):
    account_bookmark_bar = {}
    account_values["bookmark_bar"] = account_bookmark_bar
account_bookmark_bar["show_on_all_tabs"] = True

prefs_path.write_text(json.dumps(prefs, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
PY
}

if ! cdp_ok; then
  ensure_login_bookmark

  if [[ -z "${DISPLAY:-}" ]]; then
    echo "[ERROR] DISPLAY is empty. Chrome GUI cannot start."
    exit 1
  fi
  nohup "${CHROME_BIN}" \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port="${REMOTE_PORT}" \
    --user-data-dir="${CHROME_USER_DATA_DIR}" \
    --profile-directory="${CHROME_PROFILE_DIR}" \
    >"${CHROME_LOG}" 2>&1 &

  for _ in $(seq 1 20); do
    if cdp_ok; then
      break
    fi
    sleep 1
  done
fi

if ! cdp_ok; then
  echo "[ERROR] CDP port ${REMOTE_PORT} not ready."
  exit 1
fi

close_legacy_hint_tabs

if ! tab_has_target; then
  open_url_tab "${TARGET_URL}"
fi

echo "[INFO] Chrome CDP ready on 127.0.0.1:${REMOTE_PORT}"
