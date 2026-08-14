#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.worker_health import DEFAULT_FATAL_EXIT_CODE, WorkerHealthReporter

DEFAULT_TAOBAO_ACCOUNTS_CONFIG = Path.home() / ".config/kefu-workers/taobao_accounts.json"


def main() -> int:
    port = int(os.environ.get("REMOTE_PORT") or "0")
    target_url = str(os.environ.get("TARGET_URL") or "").strip()
    interval_sec = max(5.0, float(os.environ.get("BROWSER_KEEPALIVE_INTERVAL_SEC") or "15"))
    store_id = str(os.environ.get("STORE_ID") or "").strip()
    store_name = str(os.environ.get("STORE_NAME") or "").strip()
    worker_key = str(os.environ.get("WORKER_KEY") or "browser_keepalive").strip()
    worker_kind = str(os.environ.get("WORKER_KIND") or "browser").strip()
    accounts_config = Path(
        os.environ.get("TAOBAO_ACCOUNTS_CONFIG") or DEFAULT_TAOBAO_ACCOUNTS_CONFIG
    ).expanduser()

    reporter = WorkerHealthReporter.from_env(
        worker_key=worker_key,
        worker_kind=worker_kind,
        store_id=store_id,
        store_name=store_name,
    )
    reporter.start(f"starting browser keepalive port={port}")

    stopping = False

    def handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    failed_credential_fingerprint = ""
    target_open_attempted = False

    while not stopping:
        if not cdp_ok(port):
            reporter.fatal(f"chrome cdp port {port} not ready")
            return DEFAULT_FATAL_EXIT_CODE

        tabs = list_page_tabs(port)
        if tabs is None:
            reporter.degraded(f"cannot list chrome tabs port={port}")
            time.sleep(interval_sec)
            continue

        target_tabs = [tab for tab in tabs if tab_matches_target(tab, target_url)]
        login_tabs = [tab for tab in tabs if is_taobao_login_url(str(tab.get("url") or ""))]

        if target_tabs:
            close_tabs(port, login_tabs)
            failed_credential_fingerprint = ""
            target_open_attempted = False
            reporter.ready(f"target ready port={port}", progress=True)
        elif login_tabs:
            target_open_attempted = True
            close_tabs(port, login_tabs[1:])
            username, password, config_error = load_taobao_credentials(accounts_config, store_id)
            if config_error:
                reporter.degraded(config_error)
            elif not username or not password:
                reporter.manual_action(
                    f"taobao login required; credentials missing for store={store_id}"
                )
            else:
                fingerprint = credential_fingerprint(username, password)
                if fingerprint == failed_credential_fingerprint:
                    reporter.manual_action(
                        f"taobao login needs manual verification store={store_id}"
                    )
                else:
                    success, detail = auto_login_taobao(port, target_url, username, password)
                    if success:
                        failed_credential_fingerprint = ""
                        reporter.ready(f"taobao login succeeded store={store_id}", progress=True)
                    else:
                        failed_credential_fingerprint = fingerprint
                        reporter.manual_action(f"{detail} store={store_id}")
        elif target_url:
            if not target_open_attempted:
                target_open_attempted = True
                if open_target_tab(port, target_url):
                    reporter.ready(f"opened target tab port={port}", progress=True)
                else:
                    reporter.degraded(f"failed to open target tab port={port}")
            else:
                reporter.degraded(f"waiting for target tab port={port}")
        else:
            reporter.ready(f"cdp ready port={port}", progress=True)
        time.sleep(interval_sec)

    reporter.stopped("browser keepalive stopped")
    return 0


def cdp_ok(port: int) -> bool:
    try:
        request_json(port, "/json/version")
        return True
    except Exception:
        return False


def list_page_tabs(port: int) -> list[dict[str, object]] | None:
    try:
        tabs = request_json(port, "/json/list")
    except Exception:
        return None
    if not isinstance(tabs, list):
        return None
    return [tab for tab in tabs if isinstance(tab, dict) and tab.get("type") == "page"]


def tab_matches_target(tab: dict[str, object], target_url: str) -> bool:
    if not target_url:
        return False
    return url_matches_target(str(tab.get("url") or ""), target_url)


def url_matches_target(url: str, target_url: str) -> bool:
    url = str(url or "").strip()
    target_url = str(target_url or "").strip()
    if not url or not target_url:
        return False

    parsed = urllib.parse.urlsplit(target_url)
    current = urllib.parse.urlsplit(url)
    if url.startswith(target_url):
        return True
    return bool(
        parsed.scheme
        and parsed.netloc
        and current.scheme == parsed.scheme
        and current.netloc == parsed.netloc
    )


def is_taobao_login_url(url: str) -> bool:
    host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    return bool(host.endswith("taobao.com") and "login" in host)


def load_taobao_credentials(config_path: Path, store_id: str) -> tuple[str, str, str]:
    if not config_path.exists():
        return "", "", f"taobao accounts config missing: {config_path}"

    try:
        mode = config_path.stat().st_mode & 0o777
        if mode & 0o077:
            return "", "", f"taobao accounts config must be private (chmod 600): {config_path}"
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return "", "", f"invalid taobao accounts config: {type(exc).__name__}"

    normalized_id = store_id.removeprefix("tb_")
    for raw_line in lines:
        line = raw_line.lstrip()
        if not line.startswith("#@taobao_web_login|"):
            continue
        parts = line[2:].split("|", 4)
        if len(parts) != 5 or parts[0] != "taobao_web_login":
            continue
        configured_id = parts[1].strip().removeprefix("tb_")
        if configured_id != normalized_id:
            continue
        username = parts[3].strip()
        password = parts[4]
        if bool(username) != bool(password):
            return "", "", f"incomplete taobao credentials for store={store_id}"
        return username, password, ""
    return "", "", ""


def credential_fingerprint(username: str, password: str) -> str:
    value = f"{username}\0{password}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def auto_login_taobao(
    port: int,
    target_url: str,
    username: str,
    password: str,
) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False, "playwright unavailable for taobao auto login"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=10_000
            )
            page = find_login_page(browser)
            if page is None:
                return False, "taobao login page disappeared"

            page.bring_to_front()
            login_frame = wait_for_login_frame(page, timeout_sec=10.0)
            if login_frame is None:
                return False, "taobao password login form not found"

            password_tab = login_frame.locator("a.password-login-tab-item").first
            if password_tab.count() and password_tab.is_visible():
                password_tab.click(timeout=5_000)
                time.sleep(0.5)
                login_frame = wait_for_login_frame(page, timeout_sec=5.0) or login_frame

            username_input = login_frame.locator(
                "input[placeholder*='账号名'], input[aria-label*='账号名']"
            ).first
            password_input = login_frame.locator("input[type='password']").first
            submit_button = login_frame.locator(
                "button.fm-submit, button[type='submit']"
            ).first

            username_input.wait_for(state="visible", timeout=10_000)
            password_input.wait_for(state="visible", timeout=10_000)
            submit_button.wait_for(state="visible", timeout=10_000)
            username_input.fill(username)
            password_input.fill(password)
            submit_button.click(timeout=10_000)

            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                for context in browser.contexts:
                    for current_page in context.pages:
                        if url_matches_target(current_page.url, target_url):
                            return True, "taobao login succeeded"
                time.sleep(0.5)
            return False, "taobao login not completed; verification or credential check required"
    except Exception as exc:
        return False, f"taobao auto login failed: {type(exc).__name__}"


def find_login_page(browser: object) -> object | None:
    for context in getattr(browser, "contexts", []):
        for page in getattr(context, "pages", []):
            if is_taobao_login_url(getattr(page, "url", "")):
                return page
    return None


def wait_for_login_frame(page: object, timeout_sec: float) -> object | None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        named_frame = page.frame(name="alibaba-login-box")
        frames = [named_frame] if named_frame is not None else []
        frames.extend(frame for frame in page.frames if frame is not named_frame)
        for frame in frames:
            try:
                if frame.locator("input[type='password']").count():
                    return frame
            except Exception:
                continue
        time.sleep(0.25)
    return None


def close_tabs(port: int, tabs: list[dict[str, object]]) -> None:
    for tab in tabs:
        tab_id = str(tab.get("id") or "").strip()
        if not tab_id:
            continue
        try:
            request_text(port, f"/json/close/{urllib.parse.quote(tab_id, safe='')}")
        except Exception:
            pass


def open_target_tab(port: int, target_url: str) -> bool:
    encoded = urllib.parse.quote(target_url, safe=":/?&=%#,")
    try:
        request_json(port, f"/json/new?{encoded}", method="PUT")
        return True
    except Exception:
        return False


def request_json(port: int, path: str, method: str = "GET") -> object:
    text = request_text(port, path, method=method)
    return json.loads(text or "{}")


def request_text(port: int, path: str, method: str = "GET") -> str:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return resp.read().decode("utf-8", errors="ignore")


if __name__ == "__main__":
    raise SystemExit(main())
