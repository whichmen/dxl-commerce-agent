"""Shared worker health reporting and failure classification."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HEALTH_DIR = PROJECT_ROOT / "state" / "health"

STATUS_STARTING = "starting"
STATUS_READY = "ready"
STATUS_DEGRADED = "degraded"
STATUS_MANUAL_ACTION = "manual_action"
STATUS_FATAL = "fatal"
STATUS_STOPPED = "stopped"

CATEGORY_FATAL_LOCAL_STATE = "fatal_local_state"
CATEGORY_MANUAL_ACTION = "manual_action"
CATEGORY_DEGRADED = "degraded"

DEFAULT_FATAL_EXIT_CODE = 70

_FATAL_SQLITE_PATTERNS = (
    "database disk image is malformed",
    "disk i/o error",
    "file is not a database",
    "database or disk is full",
    "readonly database",
)
_MANUAL_ACTION_PATTERNS = (
    "43001",
    "会话已过期",
    "session expired",
    "登录信息已经失效",
    "鉴权失败",
    "ret=30000",
    "ret_30000",
    "扫码进入我的小店",
)


def now_ms() -> int:
    return int(time.time() * 1000)


def default_health_path(worker_key: str) -> Path:
    key = str(worker_key or "worker").strip() or "worker"
    return DEFAULT_HEALTH_DIR / f"{key}.json"


def _normalize_detail(detail: str) -> str:
    text = " ".join(str(detail or "").split())
    return text[:300]


class WorkerFatalError(RuntimeError):
    def __init__(self, detail: str, *, exit_code: int = DEFAULT_FATAL_EXIT_CODE) -> None:
        self.detail = _normalize_detail(detail) or "fatal_worker_error"
        self.exit_code = int(exit_code)
        super().__init__(self.detail)


def classify_worker_exception(exc: BaseException) -> Tuple[str, str]:
    if isinstance(exc, WorkerFatalError):
        return CATEGORY_FATAL_LOCAL_STATE, exc.detail

    raw = _normalize_detail(f"{type(exc).__name__}: {exc}")
    lowered = raw.lower()

    if isinstance(exc, sqlite3.DatabaseError) and any(token in lowered for token in _FATAL_SQLITE_PATTERNS):
        return CATEGORY_FATAL_LOCAL_STATE, raw

    if isinstance(exc, sqlite3.OperationalError) and any(token in lowered for token in _FATAL_SQLITE_PATTERNS):
        return CATEGORY_FATAL_LOCAL_STATE, raw

    if any(token in raw for token in _MANUAL_ACTION_PATTERNS) or any(
        token in lowered for token in _MANUAL_ACTION_PATTERNS
    ):
        return CATEGORY_MANUAL_ACTION, raw

    return CATEGORY_DEGRADED, raw


def load_health(path: Path | str) -> Optional[dict[str, Any]]:
    health_path = Path(path)
    try:
        payload = json.loads(health_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


class WorkerHealthReporter:
    def __init__(
        self,
        path: Path | str,
        *,
        worker_key: str,
        worker_kind: str,
        store_id: str = "",
        store_name: str = "",
        min_write_interval_ms: int = 1500,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.worker_key = str(worker_key or "").strip() or "worker"
        self.worker_kind = str(worker_kind or "").strip() or "unknown"
        self.store_id = str(store_id or "").strip()
        self.store_name = str(store_name or "").strip()
        self.min_write_interval_ms = max(200, int(min_write_interval_ms))
        self._state: dict[str, Any] = {}
        self._last_flush_ms = 0

    @classmethod
    def from_env(
        cls,
        *,
        worker_key: str,
        worker_kind: str,
        store_id: str = "",
        store_name: str = "",
    ) -> "WorkerHealthReporter":
        env_key = str(os.environ.get("WORKER_KEY") or "").strip() or str(worker_key or "").strip()
        env_kind = str(os.environ.get("WORKER_KIND") or "").strip() or str(worker_kind or "").strip()
        raw_path = str(os.environ.get("WORKER_HEALTH_PATH") or "").strip()
        health_path = Path(raw_path) if raw_path else default_health_path(env_key)
        return cls(
            health_path,
            worker_key=env_key,
            worker_kind=env_kind,
            store_id=store_id,
            store_name=store_name,
        )

    def _base_state(self, ts_ms: int) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "worker_key": self.worker_key,
            "worker_kind": self.worker_kind,
            "store_id": self.store_id,
            "store_name": self.store_name,
            "pid": os.getpid(),
            "status": STATUS_STARTING,
            "detail": "",
            "started_at_ms": ts_ms,
            "updated_at_ms": ts_ms,
            "last_progress_at_ms": 0,
        }

    def _write(self, payload: dict[str, Any]) -> None:
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def update(
        self,
        *,
        status: Optional[str] = None,
        detail: Optional[str] = None,
        progress: bool = False,
        force: bool = False,
    ) -> None:
        ts_ms = now_ms()
        state = dict(self._state or self._base_state(ts_ms))
        previous_status = str(state.get("status") or "")
        previous_detail = str(state.get("detail") or "")

        state["pid"] = os.getpid()
        state["updated_at_ms"] = ts_ms
        if status is not None:
            state["status"] = str(status or STATUS_DEGRADED).strip() or STATUS_DEGRADED
        if detail is not None:
            state["detail"] = _normalize_detail(detail)
        if progress:
            state["last_progress_at_ms"] = ts_ms

        self._state = state
        changed = (
            force
            or str(state.get("status") or "") != previous_status
            or str(state.get("detail") or "") != previous_detail
            or progress
            or ts_ms - self._last_flush_ms >= self.min_write_interval_ms
        )
        if not changed:
            return
        self._write(state)
        self._last_flush_ms = ts_ms

    def start(self, detail: str = "starting") -> None:
        self.update(status=STATUS_STARTING, detail=detail, force=True)

    def ready(self, detail: str = "ready", *, progress: bool = False) -> None:
        self.update(status=STATUS_READY, detail=detail, progress=progress)

    def degraded(self, detail: str, *, progress: bool = False) -> None:
        self.update(status=STATUS_DEGRADED, detail=detail, progress=progress)

    def manual_action(self, detail: str) -> None:
        self.update(status=STATUS_MANUAL_ACTION, detail=detail)

    def fatal(self, detail: str) -> None:
        self.update(status=STATUS_FATAL, detail=detail, force=True)

    def stopped(self, detail: str = "stopped") -> None:
        self.update(status=STATUS_STOPPED, detail=detail, force=True)

    def touch(self, *, progress: bool = False) -> None:
        self.update(progress=progress)
