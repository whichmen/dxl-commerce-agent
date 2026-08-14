#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Simple watchdog for Ubuntu workers.

Design goals:
- Single config file.
- Add/remove one worker by adding/commenting one line.
- systemd only manages this watchdog process.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from .core.worker_health import (
        STATUS_DEGRADED,
        STATUS_FATAL,
        STATUS_MANUAL_ACTION,
        default_health_path,
        load_health,
    )
    from .core.watchdog_notify import (
        ISSUE_LOGIN_REQUIRED,
        ISSUE_REPAIR_FAILED,
        WatchdogNotifier,
    )
except ImportError:  # Direct script execution from workers/
    from core.worker_health import (
        STATUS_DEGRADED,
        STATUS_FATAL,
        STATUS_MANUAL_ACTION,
        default_health_path,
        load_health,
    )
    from core.watchdog_notify import (
        ISSUE_LOGIN_REQUIRED,
        ISSUE_REPAIR_FAILED,
        WatchdogNotifier,
    )


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "state" / "logs"
DEFAULT_CONFIG_PATH = ROOT / "deploy" / "workers.watchdog.conf"

RUN_SCRIPT_BY_KIND = {
    "douyin": ROOT / "scripts" / "run_douyin_worker.sh",
    "pdd": ROOT / "scripts" / "run_pdd_worker.sh",
    "weixin": ROOT / "scripts" / "run_weixin_worker.sh",
    "kuaishou": ROOT / "scripts" / "run_kuaishou_worker.sh",
    "xhs_web": ROOT / "scripts" / "run_browser_keepalive.sh",
    "taobao": ROOT / "scripts" / "run_qianniu_accessibility_guard.sh",
    "xiaohongshu": ROOT / "scripts" / "run_qianfan_accessibility_guard.sh",
}

DEFAULT_PYTHON_BIN = sys.executable or "python3"
DEFAULT_DECISION_URL = "http://127.0.0.1:18080"
DEFAULT_TARGET_URL = {
    "douyin": "https://im.jinritemai.com/pc_seller_v2/main/workspace",
    "pdd": "https://mms.pinduoduo.com/chat-merchant/index.html#/",
    "weixin": "https://store.weixin.qq.com/shop/kf",
    "kuaishou": "https://im.kwaixiaodian.com/workbench",
    "xhs_web": "https://ark.xiaohongshu.com/",
    "taobao": "",
    "xiaohongshu": "",
}

BROWSER_KINDS = frozenset({"douyin", "pdd", "weixin", "kuaishou", "xhs_web"})
MOBILE_GUARD_KINDS = frozenset({"taobao", "xiaohongshu"})
HEALTH_CHECK_KINDS = BROWSER_KINDS | frozenset({"xiaohongshu"})
BROWSER_CLOSED_DEGRADED_PATTERNS = (
    "TargetClosedError",
    "Target page, context or browser has been closed",
)
AUTO_REMOTE_PORT_RANGES = {
    "douyin": (9220, 9239),
    "pdd": (9240, 9259),
    "weixin": (9260, 9279),
    "kuaishou": (9280, 9299),
    "xhs_web": (9300, 9399),
}
_SAFE_COMPONENT_RE = re.compile(r'[\\/:*?"<>|\s]+')


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _normalize_label(value: str) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").strip().split())


def _safe_component(value: str, *, field_name: str) -> str:
    normalized = _normalize_label(value)
    if not normalized:
        raise RuntimeError(f"{field_name} is required")
    cleaned = _SAFE_COMPONENT_RE.sub("_", normalized)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._- ")
    if not cleaned:
        raise RuntimeError(f"{field_name} is invalid")
    return cleaned


def _build_auto_key(*, kind: str, operator: str) -> str:
    return _safe_component(f"{kind}_{operator}", field_name="key")


def _build_auto_store_id(*, kind: str, store_name: str) -> str:
    return _safe_component(f"{kind}_{store_name}", field_name="store_id")


def _build_auto_profile_path(*, key: str) -> str:
    return str(ROOT / "chrome_user_data" / key)


def _build_auto_state_db(*, kind: str, operator: str) -> str:
    operator_key = _safe_component(operator, field_name="operator")
    return str(ROOT / "state" / f"{kind}_worker_{operator_key}.db")


def _allocate_auto_remote_port(
    *,
    kind: str,
    key: str,
    used_browser_ports: set[int],
) -> int:
    bounds = AUTO_REMOTE_PORT_RANGES.get(kind)
    if bounds is None:
        raise RuntimeError(f"kind={kind!r} does not support auto remote port")
    lo, hi = bounds
    span = hi - lo + 1
    start = zlib.crc32(key.encode("utf-8")) % span
    for offset in range(span):
        candidate = lo + ((start + offset) % span)
        if candidate in used_browser_ports:
            continue
        used_browser_ports.add(candidate)
        return candidate
    raise RuntimeError(f"kind={kind!r} auto remote port pool exhausted")


@dataclass
class WorkerSpec:
    key: str
    kind: str
    run_script: Path
    env: dict[str, str]
    process: Optional[subprocess.Popen] = field(default=None, init=False)
    log_handle: Optional[object] = field(default=None, init=False)
    last_start_at: float = field(default=0.0, init=False)
    last_health_issue: str = field(default="", init=False)
    fatal_restart_times: list[float] = field(default_factory=list, init=False)

    @property
    def log_path(self) -> Path:
        return LOG_DIR / f"{self.key}.log"

    @property
    def health_path(self) -> Path:
        return Path(str(self.env.get("WORKER_HEALTH_PATH") or "")).expanduser()


def _build_env(
    *,
    kind: str,
    key: str,
    operator: str,
    store_id: str,
    store_name: str,
    remote_port: int,
    profile_or_mobile_id: str,
    state_db: str,
) -> dict[str, str]:
    if kind == "taobao":
        return {
            "PYTHON_BIN": os.environ.get("PYTHON_BIN", DEFAULT_PYTHON_BIN),
            "STORE_ID": store_id,
            "STORE_NAME": store_name,
            "MOBILE_UDID": profile_or_mobile_id,
            "STATE_DB": state_db,
            "DECISION_URL": os.environ.get("DECISION_URL", DEFAULT_DECISION_URL),
            "ADB_BIN": os.environ.get("ADB_BIN", "adb"),
            "QIANNIU_GUARD_INTERVAL_SEC": os.environ.get("QIANNIU_GUARD_INTERVAL_SEC", "5"),
            "QIANNIU_ADB_TIMEOUT_SEC": os.environ.get("QIANNIU_ADB_TIMEOUT_SEC", "8"),
            "QIANNIU_FORCE_START_WORKER_APP": os.environ.get("QIANNIU_FORCE_START_WORKER_APP", "1"),
            "QIANNIU_FORCE_START_QIANNIU": os.environ.get("QIANNIU_FORCE_START_QIANNIU", "1"),
            "QIANNIU_WHITE_CHECK_INTERVAL_SEC": os.environ.get("QIANNIU_WHITE_CHECK_INTERVAL_SEC", "300"),
            "QIANNIU_WHITE_STREAK_TO_RECOVER": os.environ.get("QIANNIU_WHITE_STREAK_TO_RECOVER", "2"),
            "WORKER_OPERATOR": operator,
            "WORKER_KEY": key,
            "WORKER_KIND": kind,
            "WORKER_HEALTH_PATH": os.environ.get("WORKER_HEALTH_PATH", str(default_health_path(key))),
        }
    if kind == "xiaohongshu":
        return {
            "PYTHON_BIN": os.environ.get("PYTHON_BIN", DEFAULT_PYTHON_BIN),
            "STORE_ID": store_id,
            "STORE_NAME": store_name,
            "MOBILE_UDID": profile_or_mobile_id,
            "STATE_DB": state_db,
            "DECISION_URL": os.environ.get("DECISION_URL", DEFAULT_DECISION_URL),
            "ADB_BIN": os.environ.get("ADB_BIN", "adb"),
            "QIANFAN_GUARD_INTERVAL_SEC": os.environ.get("QIANFAN_GUARD_INTERVAL_SEC", "5"),
            "QIANFAN_ADB_TIMEOUT_SEC": os.environ.get("QIANFAN_ADB_TIMEOUT_SEC", "8"),
            "QIANFAN_FORCE_START_WORKER_APP": os.environ.get("QIANFAN_FORCE_START_WORKER_APP", "1"),
            "QIANFAN_FORCE_START_QIANFAN": os.environ.get("QIANFAN_FORCE_START_QIANFAN", "1"),
            "WORKER_OPERATOR": operator,
            "WORKER_KEY": key,
            "WORKER_KIND": kind,
            "WORKER_HEALTH_PATH": os.environ.get("WORKER_HEALTH_PATH", str(default_health_path(key))),
        }

    return {
        "PYTHON_BIN": os.environ.get("PYTHON_BIN", DEFAULT_PYTHON_BIN),
        "DECISION_URL": os.environ.get("DECISION_URL", DEFAULT_DECISION_URL),
        "DECISION_TIMEOUT_SEC": os.environ.get("DECISION_TIMEOUT_SEC", "75"),
        "MIN_REPLY_INTERVAL_SEC": os.environ.get("MIN_REPLY_INTERVAL_SEC", "0.8"),
        "FALLBACK_TEXT": os.environ.get("FALLBACK_TEXT", "稍等"),
        "STORE_ID": store_id,
        "STORE_NAME": store_name,
        "STATE_DB": state_db,
        "CHROME_BIN": os.environ.get("CHROME_BIN", "google-chrome"),
        "REMOTE_PORT": str(remote_port),
        "CHROME_USER_DATA_DIR": profile_or_mobile_id,
        "CHROME_PROFILE_DIR": os.environ.get("CHROME_PROFILE_DIR", "Default"),
        "TARGET_URL": os.environ.get("TARGET_URL", DEFAULT_TARGET_URL[kind]),
        "CHROME_LOG": os.environ.get("CHROME_LOG", f"/tmp/chrome_{key}.log"),
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
        "XAUTHORITY": os.environ.get("XAUTHORITY", str(Path.home() / ".Xauthority")),
        "WORKER_OPERATOR": operator,
        "WORKER_KEY": key,
        "WORKER_KIND": kind,
        "WORKER_HEALTH_PATH": os.environ.get("WORKER_HEALTH_PATH", str(default_health_path(key))),
    }


def _build_worker_spec(
    *,
    kind: str,
    key: str,
    operator: str,
    store_id: str,
    store_name: str,
    remote_port: int,
    profile_or_mobile_id: str,
    state_db: str,
    lineno: int,
) -> WorkerSpec:
    if kind not in RUN_SCRIPT_BY_KIND:
        raise RuntimeError(
            f"line {lineno}: unsupported kind={kind!r}, expected douyin/pdd/weixin/kuaishou/xhs_web/taobao/xiaohongshu"
        )
    if not key:
        raise RuntimeError(f"line {lineno}: key is required")
    if not store_id:
        raise RuntimeError(f"line {lineno}: store_id is required")
    if not profile_or_mobile_id:
        if kind in MOBILE_GUARD_KINDS:
            raise RuntimeError(f"line {lineno}: mobile_udid is required")
        raise RuntimeError(f"line {lineno}: chrome_user_data_dir is required")
    if not state_db:
        raise RuntimeError(f"line {lineno}: state_db is required")
    if remote_port <= 0:
        raise RuntimeError(f"line {lineno}: remote_port must > 0")

    env = _build_env(
        kind=kind,
        key=key,
        operator=operator or key,
        store_id=store_id,
        store_name=store_name or store_id,
        remote_port=remote_port,
        profile_or_mobile_id=profile_or_mobile_id,
        state_db=state_db,
    )

    return WorkerSpec(
        key=key,
        kind=kind,
        run_script=RUN_SCRIPT_BY_KIND[kind],
        env=env,
    )


def _parse_worker_line(
    line: str,
    lineno: int,
    *,
    used_browser_ports: set[int],
) -> WorkerSpec:
    # format:
    # shorthand browser worker:
    #   kind|operator|store_name
    # shorthand mobile guard:
    #   kind|operator|store_name|mobile_udid
    # legacy format:
    #   kind|key|operator|store_id|store_name|remote_port|profile_or_mobile_id|state_db
    parts = [x.strip() for x in line.split("|")]
    if len(parts) == 3:
        kind, operator, store_name = parts
        if kind not in BROWSER_KINDS:
            raise RuntimeError(
                f"line {lineno}: shorthand only supports douyin/pdd/weixin/kuaishou/xhs_web; "
                f"{kind or 'empty'} still needs the full 8-field format"
            )
        if not operator:
            raise RuntimeError(f"line {lineno}: operator is required")
        if not store_name:
            raise RuntimeError(f"line {lineno}: store_name is required")
        key = _build_auto_key(kind=kind, operator=operator)
        remote_port = _allocate_auto_remote_port(
            kind=kind,
            key=key,
            used_browser_ports=used_browser_ports,
        )
        return _build_worker_spec(
            kind=kind,
            key=key,
            operator=_normalize_label(operator),
            store_id=_build_auto_store_id(kind=kind, store_name=store_name),
            store_name=_normalize_label(store_name),
            remote_port=remote_port,
            profile_or_mobile_id=_build_auto_profile_path(key=key),
            state_db=_build_auto_state_db(kind=kind, operator=operator),
            lineno=lineno,
        )

    if len(parts) == 4:
        kind, operator, store_name, mobile_udid = parts
        if kind not in MOBILE_GUARD_KINDS:
            raise RuntimeError(
                f"line {lineno}: 4-field shorthand only supports taobao/xiaohongshu; "
                f"{kind or 'empty'} should use the 3-field browser shorthand or full 8-field format"
            )
        if not operator:
            raise RuntimeError(f"line {lineno}: operator is required")
        if not store_name:
            raise RuntimeError(f"line {lineno}: store_name is required")
        if not mobile_udid:
            raise RuntimeError(f"line {lineno}: mobile_udid is required")
        return _build_worker_spec(
            kind=kind,
            key=_build_auto_key(kind=kind, operator=operator),
            operator=_normalize_label(operator),
            store_id=_build_auto_store_id(kind=kind, store_name=store_name),
            store_name=_normalize_label(store_name),
            remote_port=1,
            profile_or_mobile_id=mobile_udid,
            state_db=_build_auto_state_db(kind=kind, operator=operator),
            lineno=lineno,
        )

    if len(parts) != 8:
        raise RuntimeError(
            f"line {lineno}: invalid field count={len(parts)}, expected 3, 4 or 8"
        )

    kind, key, operator, store_id, store_name, remote_port_raw, profile_or_mobile_id, state_db = parts
    try:
        remote_port = int(remote_port_raw)
    except Exception as exc:
        raise RuntimeError(f"line {lineno}: invalid remote_port={remote_port_raw!r}") from exc
    return _build_worker_spec(
        kind=kind,
        key=key,
        operator=operator,
        store_id=store_id,
        store_name=store_name,
        remote_port=remote_port,
        profile_or_mobile_id=profile_or_mobile_id,
        state_db=state_db,
        lineno=lineno,
    )


def _collect_reserved_browser_ports(lines: list[tuple[int, str]]) -> set[int]:
    reserved: set[int] = set()
    for lineno, line in lines:
        parts = [x.strip() for x in line.split("|")]
        if len(parts) != 8:
            continue
        kind = parts[0]
        remote_port_raw = parts[5]
        if kind not in BROWSER_KINDS:
            continue
        try:
            reserved.add(int(remote_port_raw))
        except Exception as exc:
            raise RuntimeError(f"line {lineno}: invalid remote_port={remote_port_raw!r}") from exc
    return reserved


def load_workers(config_path: Path) -> list[WorkerSpec]:
    if not config_path.exists():
        raise RuntimeError(f"config not found: {config_path}")

    active_lines: list[tuple[int, str]] = []
    for lineno, raw in enumerate(config_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        active_lines.append((lineno, line))

    used_browser_ports = _collect_reserved_browser_ports(active_lines)
    workers: list[WorkerSpec] = []
    for lineno, line in active_lines:
        workers.append(
            _parse_worker_line(
                line,
                lineno,
                used_browser_ports=used_browser_ports,
            )
        )

    if not workers:
        raise RuntimeError("no enabled workers in config")
    return workers


def _validate_workers(workers: list[WorkerSpec]) -> None:
    keys: set[str] = set()
    ports: set[str] = set()
    browser_profiles: set[str] = set()
    mobile_udids: set[str] = set()
    dbs: set[str] = set()
    missing_scripts = sorted({str(w.run_script) for w in workers if not w.run_script.exists()})
    if missing_scripts:
        raise RuntimeError(f"run script not found: {', '.join(missing_scripts)}")

    for w in workers:
        key = w.key
        port = str(w.env.get("REMOTE_PORT") or "").strip()
        profile = str(w.env.get("CHROME_USER_DATA_DIR") or "").strip()
        mobile_udid = str(w.env.get("MOBILE_UDID") or "").strip()
        db_path = str(w.env.get("STATE_DB") or "").strip()
        if not key or not db_path:
            raise RuntimeError(f"worker {w.key} missing required fields")
        if key in keys:
            raise RuntimeError(f"duplicate key: {key}")
        if db_path in dbs:
            raise RuntimeError(f"duplicate STATE_DB: {db_path}")
        keys.add(key)
        dbs.add(db_path)

        if w.kind in MOBILE_GUARD_KINDS:
            if not mobile_udid:
                raise RuntimeError(f"worker {w.key} missing MOBILE_UDID")
            if mobile_udid in mobile_udids:
                raise RuntimeError(f"duplicate MOBILE_UDID: {mobile_udid}")
            mobile_udids.add(mobile_udid)
            continue

        if not port or not profile:
            raise RuntimeError(f"worker {w.key} missing required browser fields")
        if port in ports:
            raise RuntimeError(f"duplicate REMOTE_PORT: {port}")
        if profile in browser_profiles:
            raise RuntimeError(f"duplicate CHROME_USER_DATA_DIR: {profile}")
        ports.add(port)
        browser_profiles.add(profile)


def _start_worker(spec: WorkerSpec) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if spec.kind not in MOBILE_GUARD_KINDS:
        Path(spec.env["CHROME_USER_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(spec.env["STATE_DB"]).parent.mkdir(parents=True, exist_ok=True)
    spec.health_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        spec.health_path.unlink(missing_ok=True)
    except Exception:
        pass

    if spec.log_handle:
        try:
            spec.log_handle.close()
        except Exception:
            pass
        spec.log_handle = None

    log_f = open(spec.log_path, "ab")
    env = os.environ.copy()
    env.update(spec.env)
    proc = subprocess.Popen(
        ["/bin/bash", str(spec.run_script)],
        cwd=str(ROOT),
        env=env,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    spec.process = proc
    spec.log_handle = log_f
    spec.last_start_at = time.time()
    spec.last_health_issue = ""
    if spec.kind in MOBILE_GUARD_KINDS:
        _log(
            f"[INFO][{spec.key}] started pid={proc.pid} "
            f"kind={spec.kind} mode=accessibility_guard "
            f"udid={spec.env.get('MOBILE_UDID')}"
        )
    else:
        _log(
            f"[INFO][{spec.key}] started pid={proc.pid} "
            f"kind={spec.kind} port={spec.env.get('REMOTE_PORT')} "
            f"profile={spec.env.get('CHROME_USER_DATA_DIR')}"
        )
    _log(f"[INFO][{spec.key}] worker log file: {spec.log_path}")


def _stop_worker(
    spec: WorkerSpec,
    sig: int = signal.SIGTERM,
    *,
    wait_sec: float = 5.0,
    force_kill: bool = True,
) -> None:
    proc = spec.process
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        deadline = time.time() + max(0.0, float(wait_sec))
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if force_kill and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            deadline = time.time() + 2.0
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
    spec.process = None
    if spec.log_handle:
        try:
            spec.log_handle.close()
        except Exception:
            pass
        spec.log_handle = None
    spec.last_health_issue = ""


def _evaluate_worker_health(
    spec: WorkerSpec,
    *,
    start_grace_sec: float,
    stale_sec: float,
) -> Optional[tuple[str, str]]:
    if spec.kind not in HEALTH_CHECK_KINDS:
        return None

    health = load_health(spec.health_path)
    since_start = max(0.0, time.time() - spec.last_start_at)
    if health is None:
        if since_start <= start_grace_sec:
            return None
        return ("restart", "health_missing")

    updated_at_ms = 0
    try:
        updated_at_ms = int(health.get("updated_at_ms") or 0)
    except Exception:
        updated_at_ms = 0

    status = str(health.get("status") or "").strip()
    detail = str(health.get("detail") or "").strip()

    if status == STATUS_MANUAL_ACTION:
        return ("hold", f"manual_action:{detail or 'pending_login'}")

    if status == STATUS_FATAL:
        return ("restart", f"health_fatal:{detail or 'fatal'}")

    if status == STATUS_DEGRADED and any(token in detail for token in BROWSER_CLOSED_DEGRADED_PATTERNS):
        return ("restart", f"health_degraded_browser_closed:{detail or 'browser_closed'}")

    if updated_at_ms > 0:
        age_sec = max(0.0, time.time() - (updated_at_ms / 1000.0))
        if age_sec > stale_sec:
            return ("restart", f"health_stale:{int(age_sec)}s")
    elif since_start > start_grace_sec:
        return ("restart", "health_missing_timestamp")

    return None


def _restart_running_worker(spec: WorkerSpec, *, reason: str) -> None:
    _log(f"[WARN][{spec.key}] restarting, reason={reason}")
    _stop_worker(spec, sig=signal.SIGTERM, wait_sec=5.0, force_kill=True)
    _start_worker(spec)


def _issue_key(action: str, reason: str) -> str:
    text = str(reason or "").strip()
    if action == "hold" and text.startswith("manual_action:"):
        return "manual_action"
    if text.startswith("health_fatal:"):
        return "health_fatal"
    if text.startswith("process_exited:70"):
        return "process_exited:70"
    if text.startswith("fatal_restart_limited:"):
        return "fatal_restart_limited"
    return text


def _prune_restart_times(spec: WorkerSpec, *, now_ts: float, window_sec: float) -> None:
    cutoff = now_ts - max(1.0, float(window_sec))
    spec.fatal_restart_times = [ts for ts in spec.fatal_restart_times if ts >= cutoff]


def _should_hold_fatal_restart(
    spec: WorkerSpec,
    *,
    now_ts: float,
    limit: int,
    window_sec: float,
) -> bool:
    _prune_restart_times(spec, now_ts=now_ts, window_sec=window_sec)
    return len(spec.fatal_restart_times) >= max(1, int(limit))


def _mark_fatal_restart(
    spec: WorkerSpec,
    *,
    now_ts: float,
    window_sec: float,
) -> None:
    _prune_restart_times(spec, now_ts=now_ts, window_sec=window_sec)
    spec.fatal_restart_times.append(now_ts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="kefu worker watchdog")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="workers config file path",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()

    try:
        workers = load_workers(config_path)
        _validate_workers(workers)
    except Exception as exc:
        _log(f"[ERROR] worker config invalid: {exc}")
        return 2

    interval_sec = float(os.environ.get("WATCHDOG_INTERVAL_SEC", "10"))
    cooldown_sec = float(os.environ.get("WATCHDOG_RESTART_COOLDOWN_SEC", "8"))
    health_start_grace_sec = float(os.environ.get("WATCHDOG_HEALTH_START_GRACE_SEC", "25"))
    health_stale_sec = float(os.environ.get("WATCHDOG_HEALTH_STALE_SEC", "45"))
    fatal_restart_limit = int(os.environ.get("WATCHDOG_FATAL_RESTART_LIMIT", "3"))
    fatal_restart_window_sec = float(os.environ.get("WATCHDOG_FATAL_RESTART_WINDOW_SEC", "180"))
    notifier = WatchdogNotifier.from_env()
    stopping = False

    def _handle_stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        _log(f"[INFO] signal received: {signum}")

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    _log(
        f"[INFO] watchdog started, workers={len(workers)}, "
        f"config={config_path}, interval={interval_sec:.1f}s, cooldown={cooldown_sec:.1f}s, "
        f"fatal_limit={fatal_restart_limit}/{int(fatal_restart_window_sec)}s"
    )

    while not stopping:
        for spec in workers:
            proc = spec.process
            if proc is None:
                _log(f"[WARN][{spec.key}] restarting, reason=process_not_running")
                _start_worker(spec)
                continue

            code = proc.poll()
            if code is None:
                health = load_health(spec.health_path)
                health_action = _evaluate_worker_health(
                    spec,
                    start_grace_sec=health_start_grace_sec,
                    stale_sec=health_stale_sec,
                )
                if health_action is None:
                    if isinstance(health, dict) and str(health.get("status") or "").strip() == "ready":
                        ok, result = notifier.record_ready(
                            spec=spec,
                            ready_detail=str(health.get("detail") or "ready"),
                        )
                        if ok:
                            _log(f"[INFO][{spec.key}] notify sent, kind=recovered")
                        elif result not in {"normal", "retry_later"}:
                            _log(f"[WARN][{spec.key}] notify skipped, kind=recovered, reason={result}")
                    spec.last_health_issue = ""
                    continue
                action, reason = health_action
                issue_key = _issue_key(action, reason)
                if action == "hold":
                    if spec.last_health_issue != issue_key:
                        _log(f"[WARN][{spec.key}] hold, reason={reason}")
                    if reason.startswith("manual_action:"):
                        ok, result = notifier.record_issue(
                            spec=spec,
                            issue_kind=ISSUE_LOGIN_REQUIRED,
                            detail=reason.split(":", 1)[1] if ":" in reason else reason,
                        )
                        if ok:
                            _log(f"[INFO][{spec.key}] notify sent, kind=login_required")
                        elif result not in {"already_notified", "retry_later"}:
                            _log(f"[WARN][{spec.key}] notify skipped, kind=login_required, reason={result}")
                    elif reason.startswith("fatal_restart_limited:"):
                        detail = ""
                        if isinstance(health, dict):
                            detail = str(health.get("detail") or "").strip()
                        ok, result = notifier.record_issue(
                            spec=spec,
                            issue_kind=ISSUE_REPAIR_FAILED,
                            detail=detail or reason,
                        )
                        if ok:
                            _log(f"[INFO][{spec.key}] notify sent, kind=repair_failed")
                        elif result not in {"already_notified", "retry_later"}:
                            _log(f"[WARN][{spec.key}] notify skipped, kind=repair_failed, reason={result}")
                    spec.last_health_issue = issue_key
                    continue

                now_ts = time.time()
                if reason.startswith("health_fatal:"):
                    if _should_hold_fatal_restart(
                        spec,
                        now_ts=now_ts,
                        limit=fatal_restart_limit,
                        window_sec=fatal_restart_window_sec,
                    ):
                        hold_reason = (
                            f"fatal_restart_limited:{len(spec.fatal_restart_times)}/"
                            f"{fatal_restart_limit} within {int(fatal_restart_window_sec)}s"
                        )
                        hold_key = _issue_key("hold", hold_reason)
                        if spec.last_health_issue != hold_key:
                            _log(f"[WARN][{spec.key}] hold, reason={hold_reason}")
                        spec.last_health_issue = hold_key
                        continue
                    _mark_fatal_restart(
                        spec,
                        now_ts=now_ts,
                        window_sec=fatal_restart_window_sec,
                    )

                since = now_ts - spec.last_start_at
                if since < cooldown_sec:
                    if spec.last_health_issue != issue_key:
                        _log(
                            f"[WARN][{spec.key}] restart cooldown active "
                            f"({int(cooldown_sec - since)}s), reason={reason}"
                        )
                    spec.last_health_issue = issue_key
                    continue

                _restart_running_worker(spec, reason=reason)
                continue

            now_ts = time.time()
            issue_key = _issue_key("restart", f"process_exited:{code}")
            if code == 70:
                if _should_hold_fatal_restart(
                    spec,
                    now_ts=now_ts,
                    limit=fatal_restart_limit,
                    window_sec=fatal_restart_window_sec,
                ):
                    hold_reason = (
                        f"fatal_restart_limited:{len(spec.fatal_restart_times)}/"
                        f"{fatal_restart_limit} within {int(fatal_restart_window_sec)}s"
                    )
                    hold_key = _issue_key("hold", hold_reason)
                    if spec.last_health_issue != hold_key:
                        _log(f"[WARN][{spec.key}] hold, reason={hold_reason}")
                    health = load_health(spec.health_path)
                    detail = ""
                    if isinstance(health, dict):
                        detail = str(health.get("detail") or "").strip()
                    ok, result = notifier.record_issue(
                        spec=spec,
                        issue_kind=ISSUE_REPAIR_FAILED,
                        detail=detail or hold_reason,
                    )
                    if ok:
                        _log(f"[INFO][{spec.key}] notify sent, kind=repair_failed")
                    elif result not in {"already_notified", "retry_later"}:
                        _log(f"[WARN][{spec.key}] notify skipped, kind=repair_failed, reason={result}")
                    spec.last_health_issue = hold_key
                    continue
                _mark_fatal_restart(
                    spec,
                    now_ts=now_ts,
                    window_sec=fatal_restart_window_sec,
                )

            since = now_ts - spec.last_start_at
            if since < cooldown_sec:
                _log(
                    f"[WARN][{spec.key}] restart cooldown active "
                    f"({int(cooldown_sec - since)}s), reason=process_exited:{code}"
                )
                continue

            _log(f"[WARN][{spec.key}] restarting, reason=process_exited:{code}")
            spec.last_health_issue = issue_key
            _start_worker(spec)

        time.sleep(interval_sec)

    for spec in workers:
        _stop_worker(spec, sig=signal.SIGTERM)
    _log("[INFO] watchdog stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
