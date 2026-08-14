"""Watchdog notifications via the local kefu-ops tool endpoint."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = PROJECT_ROOT / "state" / "watchdog_notify_state.json"

ISSUE_LOGIN_REQUIRED = "login_required"
ISSUE_REPAIR_FAILED = "repair_failed"

PHASE_NORMAL = "normal"
PHASE_ISSUE = "issue"
PHASE_RESOLVED_PENDING = "resolved_pending"


def now_ms() -> int:
    return int(time.time() * 1000)


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _format_duration(ms: int) -> str:
    total_sec = max(0, int(ms / 1000))
    minutes, sec = divmod(total_sec, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分{sec}秒"
    if minutes > 0:
        return f"{minutes}分{sec}秒"
    return f"{sec}秒"


def _display_platform(kind: str) -> str:
    mapping = {
        "weixin": "视频号",
        "douyin": "抖音",
        "pdd": "拼多多",
        "kuaishou": "快手",
        "xhs_web": "小红书网页",
        "taobao": "淘宝",
        "xiaohongshu": "小红书",
    }
    key = str(kind or "").strip().lower()
    return mapping.get(key, str(kind or "").strip() or "未知")


def _display_ready_status(detail: str) -> str:
    value = _safe_text(detail, limit=120)
    lowered = value.lower()
    if lowered in {"", "ready", "ok", "healthy"}:
        return "已恢复正常"
    return value


def _display_issue_label(issue_kind: str, detail: str) -> str:
    if issue_kind != ISSUE_LOGIN_REQUIRED:
        return "自动修复失败"
    value = _safe_text(detail, limit=120)
    if "离线" in value:
        return "客服离线"
    if "登录" in value or "验证码" in value:
        return "需要重新登录"
    return "需要人工处理"


class WatchdogNotifier:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        to_user: str,
        app_key: str,
        api_key: str,
        state_path: Path | str = DEFAULT_STATE_PATH,
        retry_sec: float = 300.0,
        ready_stable_sec: float = 120.0,
        machine_name: str = "",
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = str(base_url or "").rstrip("/")
        self.to_user = str(to_user or "").strip()
        self.app_key = str(app_key or "").strip() or "default"
        self.api_key = str(api_key or "").strip()
        self.state_path = Path(state_path)
        self.retry_sec = max(10.0, float(retry_sec))
        self.ready_stable_sec = max(15.0, float(ready_stable_sec))
        self.machine_name = str(machine_name or "").strip() or socket.gethostname()
        self._state = self._load_state()

    @classmethod
    def from_env(cls) -> "WatchdogNotifier":
        enabled_raw = str(os.environ.get("WATCHDOG_NOTIFY_ENABLED", "1")).strip().lower()
        enabled = enabled_raw not in {"0", "false", "no", "off"}
        decision_url = str(os.environ.get("DECISION_URL", "http://127.0.0.1:18080")).strip()
        base_url = str(os.environ.get("WATCHDOG_NOTIFY_BASE_URL") or decision_url).strip()
        to_user = str(os.environ.get("WATCHDOG_NOTIFY_TOUSER", "@all")).strip()
        app_key = str(os.environ.get("WATCHDOG_NOTIFY_APP_KEY", "default")).strip() or "default"
        api_key = str(
            os.environ.get("WATCHDOG_NOTIFY_API_KEY")
            or os.environ.get("CLAWBOT_TOOL_API_KEY")
            or ""
        ).strip()
        state_path = Path(
            str(os.environ.get("WATCHDOG_NOTIFY_STATE_PATH") or DEFAULT_STATE_PATH)
        ).expanduser()
        retry_sec = float(os.environ.get("WATCHDOG_NOTIFY_RETRY_SEC", "300") or "300")
        ready_stable_sec = float(os.environ.get("WATCHDOG_NOTIFY_READY_STABLE_SEC", "120") or "120")
        machine_name = str(os.environ.get("WATCHDOG_NOTIFY_MACHINE") or "").strip()
        return cls(
            enabled=enabled,
            base_url=base_url,
            to_user=to_user,
            app_key=app_key,
            api_key=api_key,
            state_path=state_path,
            retry_sec=retry_sec,
            ready_stable_sec=ready_stable_sec,
            machine_name=machine_name,
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        workers = payload.get("workers")
        if not isinstance(workers, dict):
            workers = {}
        return {
            "schema_version": 1,
            "workers": workers,
        }

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(self._state, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.state_path)

    def _worker_state(self, worker_key: str) -> dict[str, Any]:
        workers = self._state.setdefault("workers", {})
        payload = workers.get(worker_key)
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("phase", PHASE_NORMAL)
        payload.setdefault("issue_kind", "")
        payload.setdefault("issue_detail", "")
        payload.setdefault("issue_since_ms", 0)
        payload.setdefault("issue_notified_at_ms", 0)
        payload.setdefault("resolved_status", "")
        payload.setdefault("resolved_at_ms", 0)
        payload.setdefault("last_attempt_at_ms", 0)
        workers[worker_key] = payload
        return payload

    def _should_retry(self, worker_state: dict[str, Any], *, ts_ms: int) -> bool:
        last_attempt = int(worker_state.get("last_attempt_at_ms") or 0)
        return ts_ms - last_attempt >= int(self.retry_sec * 1000)

    def _post_tool(self, *, content: str) -> tuple[bool, str]:
        if not self.enabled:
            return False, "notify_disabled"
        if not self.base_url:
            return False, "notify_base_url_missing"
        if not self.to_user:
            return False, "notify_touser_missing"

        body = {
            "args": {
                "app_key": self.app_key,
                "touser": self.to_user,
                "content": content,
            }
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url.rstrip('/')}/v1/tools/wecom_notify"
        req = urllib.request.Request(url=url, data=data, method="POST")
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        if self.api_key:
            req.add_header("X-Api-Key", self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            return False, f"http_{exc.code}:{_safe_text(detail, limit=240)}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}:{_safe_text(exc, limit=240)}"

        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            parsed = {}
        if not isinstance(parsed, dict):
            return False, "invalid_json"
        if bool(parsed.get("ok")):
            return True, "ok"
        return False, _safe_text(parsed.get("message") or "notify_failed", limit=240)

    def _build_issue_message(
        self,
        *,
        spec: Any,
        issue_kind: str,
        detail: str,
        issue_since_ms: int,
    ) -> str:
        title = "客服监控告警"
        store_name = str(spec.env.get("STORE_NAME") or spec.env.get("STORE_ID") or "").strip()
        issue_label = _display_issue_label(issue_kind, detail)
        lines = [
            f"⚠️【{title}】",
            f"类型：{issue_label}",
            f"渠道：{_display_platform(spec.kind)}",
        ]
        if store_name:
            lines.append(f"店铺：{store_name}")
        lines.append(f"实例：{spec.key}")
        lines.extend(
            [
                "",
                f"持续：{_format_duration(now_ms() - issue_since_ms)}",
                f"主机：{self.machine_name}",
                "",
                "详情：",
                _safe_text(detail),
            ]
        )
        return "\n".join(lines).strip()

    def _build_resolution_message(
        self,
        *,
        spec: Any,
        issue_kind: str,
        issue_detail: str,
        issue_since_ms: int,
        ready_detail: str,
    ) -> str:
        store_name = str(spec.env.get("STORE_NAME") or spec.env.get("STORE_ID") or "").strip()
        issue_label = _display_issue_label(issue_kind, issue_detail)
        lines = [
            "✅【客服监控恢复】",
            f"类型：{issue_label}已恢复",
            f"渠道：{_display_platform(spec.kind)}",
        ]
        if store_name:
            lines.append(f"店铺：{store_name}")
        lines.append(f"实例：{spec.key}")
        lines.extend(
            [
                "",
                f"异常持续：{_format_duration(now_ms() - issue_since_ms)}",
                f"主机：{self.machine_name}",
                f"恢复状态：{_display_ready_status(ready_detail)}",
                "",
                "原问题：",
                _safe_text(issue_detail),
            ]
        )
        return "\n".join(lines).strip()

    def record_issue(self, *, spec: Any, issue_kind: str, detail: str) -> tuple[bool, str]:
        worker_state = self._worker_state(spec.key)
        ts_ms = now_ms()

        if worker_state.get("phase") != PHASE_ISSUE or worker_state.get("issue_kind") != issue_kind:
            worker_state["phase"] = PHASE_ISSUE
            worker_state["issue_kind"] = issue_kind
            worker_state["issue_detail"] = _safe_text(detail)
            worker_state["issue_since_ms"] = ts_ms
            worker_state["issue_notified_at_ms"] = 0
            worker_state["resolved_status"] = ""
            worker_state["resolved_at_ms"] = 0
            worker_state["last_attempt_at_ms"] = 0
        else:
            worker_state["issue_detail"] = _safe_text(detail)

        if not self._should_retry(worker_state, ts_ms=ts_ms):
            self._save_state()
            return False, "retry_later"

        worker_state["last_attempt_at_ms"] = ts_ms
        ok, result = self._post_tool(
            content=self._build_issue_message(
                spec=spec,
                issue_kind=issue_kind,
                detail=worker_state.get("issue_detail") or detail,
                issue_since_ms=int(worker_state.get("issue_since_ms") or ts_ms),
            )
        )
        if ok:
            worker_state["issue_notified_at_ms"] = ts_ms
        self._save_state()
        return ok, result

    def record_ready(self, *, spec: Any, ready_detail: str) -> tuple[bool, str]:
        worker_state = self._worker_state(spec.key)
        phase = str(worker_state.get("phase") or PHASE_NORMAL)
        if phase == PHASE_NORMAL:
            return False, "normal"

        ts_ms = now_ms()
        if phase == PHASE_ISSUE:
            worker_state["phase"] = PHASE_RESOLVED_PENDING
            worker_state["resolved_status"] = _safe_text(ready_detail, limit=120)
            worker_state["resolved_at_ms"] = ts_ms
            worker_state["last_attempt_at_ms"] = 0

        if worker_state.get("phase") != PHASE_RESOLVED_PENDING:
            self._save_state()
            return False, "not_resolved_pending"
        worker_state["resolved_status"] = _safe_text(ready_detail, limit=120)
        resolved_at_ms = int(worker_state.get("resolved_at_ms") or ts_ms)
        if ts_ms - resolved_at_ms < int(self.ready_stable_sec * 1000):
            self._save_state()
            return False, "wait_stable"
        if not self._should_retry(worker_state, ts_ms=ts_ms):
            self._save_state()
            return False, "retry_later"

        worker_state["last_attempt_at_ms"] = ts_ms
        ok, result = self._post_tool(
            content=self._build_resolution_message(
                spec=spec,
                issue_kind=str(worker_state.get("issue_kind") or ""),
                issue_detail=str(worker_state.get("issue_detail") or ""),
                issue_since_ms=int(worker_state.get("issue_since_ms") or ts_ms),
                ready_detail=str(worker_state.get("resolved_status") or ready_detail),
            )
        )
        if ok:
            workers = self._state.setdefault("workers", {})
            workers[spec.key] = {
                "phase": PHASE_NORMAL,
                "issue_kind": "",
                "issue_detail": "",
                "issue_since_ms": 0,
                "issue_notified_at_ms": 0,
                "resolved_status": "",
                "resolved_at_ms": 0,
                "last_attempt_at_ms": 0,
            }
        self._save_state()
        return ok, result
