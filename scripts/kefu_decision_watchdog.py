#!/usr/bin/env python3
"""Active watchdog for the kefu decision path.

This checks the real /v1/decide path, not just /health.  If the decision
request times out or returns an invalid response, it restarts the OpenClaw
gateway and the kefu bridge so systemd can recover from a live-but-stuck
process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any


DECISION_URL = os.environ.get("KEFU_DECISION_URL", "http://127.0.0.1:18080").rstrip("/")
TIMEOUT_SEC = float(os.environ.get("KEFU_DECISION_TIMEOUT_SEC", "45"))
DECISION_API_KEY = str(
    os.environ.get("KEFU_DECISION_API_KEY") or os.environ.get("CLAWBOT_API_KEY") or ""
).strip()
BRIDGE_UNIT = os.environ.get("KEFU_DECISION_BRIDGE_UNIT", "kefu-clawbot.service")
GATEWAY_UNIT = os.environ.get("KEFU_DECISION_GATEWAY_USER_UNIT", "openclaw-gateway.service")
RESTART_GATEWAY = os.environ.get("KEFU_DECISION_RESTART_GATEWAY", "1").strip() != "0"


def _post_json(url: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
    }
    if DECISION_API_KEY:
        headers["X-Api-Key"] = DECISION_API_KEY
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("decision_response_not_object")
    return parsed


def _build_payload() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    session_key = "pdd:watchdog_store:kefu_decision_watchdog"
    return {
        "event": {
            "tenant_id": "watchdog",
            "platform": "pdd",
            "store_id": "watchdog_store",
            "store_name": "客服决策健康检查",
            "customer_id": "watchdog_customer",
            "platform_nickname": "kefu_decision_watchdog",
            "message_id": f"kefu_decision_watchdog_{now_ms}",
            "message_type": "text",
            "text": "这个衣服含棉多少",
            "media_url": "",
            "timestamp_ms": now_ms,
            "raw": {"watchdog": True},
            "role": "customer",
            "session_key": session_key,
        }
    }


def _validate_decision(data: dict[str, Any]) -> None:
    action = str(data.get("action") or "").strip()
    if action not in {"send", "skip", "manual"}:
        raise RuntimeError(f"invalid_action:{action or 'empty'}")
    if action == "send" and not str(data.get("reply_text") or "").strip():
        raise RuntimeError("empty_reply_text")


def _run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=False, timeout=60)


def _run_ok(cmd: list[str], *, timeout_sec: float = 60.0) -> bool:
    print("+ " + " ".join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, check=False, timeout=timeout_sec)
    except Exception as exc:  # noqa: BLE001
        print(f"command_failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return False
    return proc.returncode == 0


def _systemctl_main_pid(unit: str) -> int:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"],
            check=False,
            timeout=10,
            text=True,
            capture_output=True,
        )
    except Exception:
        return 0
    if proc.returncode != 0:
        return 0
    try:
        return int((proc.stdout or "").strip() or "0")
    except ValueError:
        return 0


def _restart_gateway() -> None:
    _run(["systemctl", "--user", "restart", GATEWAY_UNIT])


def _restart_bridge() -> None:
    if _run_ok(["systemctl", "--user", "restart", BRIDGE_UNIT], timeout_sec=15):
        return

    pid = _systemctl_main_pid(BRIDGE_UNIT)
    if pid <= 1:
        print(f"bridge_restart_failed: main_pid_not_found unit={BRIDGE_UNIT}", file=sys.stderr, flush=True)
        return
    print(f"bridge_restart_fallback: kill SIGTERM pid={pid}", flush=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001
        print(f"bridge_kill_failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _restart_services() -> None:
    if RESTART_GATEWAY:
        _restart_gateway()
    _restart_bridge()


def main() -> int:
    try:
        data = _post_json(f"{DECISION_URL}/v1/decide", _build_payload(), TIMEOUT_SEC)
        _validate_decision(data)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"decision_failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        _restart_services()
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"decision_failed_unexpected: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        _restart_services()
        return 1

    reason = str(data.get("reason") or "").strip()
    print(f"decision_ok action={data.get('action')} reason={reason}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
