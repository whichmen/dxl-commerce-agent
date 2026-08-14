"""Immediate alerting for decision-service failures and recoveries."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = PROJECT_ROOT / "state" / "decision_alerts"

PHASE_NORMAL = "normal"
PHASE_ISSUE = "issue"
PHASE_RECOVERING = "recovering"


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
        "taobao": "淘宝",
        "xiaohongshu": "小红书",
    }
    key = str(kind or "").strip().lower()
    return mapping.get(key, str(kind or "").strip() or "未知")


def _env_first(*keys: str, default: str = "") -> str:
    for key in keys:
        value = str(os.environ.get(key, "") or "").strip()
        if value:
            return value
    return str(default or "").strip()


class DecisionAlertNotifier:
    """Per-worker alert sender with local state to throttle repeated failures."""

    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str,
        api_key: str,
        app_key: str,
        to_user: str,
        worker_key: str,
        worker_kind: str,
        store_id: str,
        store_name: str,
        state_dir: Path | str = DEFAULT_STATE_DIR,
        retry_sec: float = 300.0,
        machine_name: str = "",
    ) -> None:
        self.enabled = bool(enabled)
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.app_key = str(app_key or "").strip() or "default"
        self.to_user = str(to_user or "").strip() or "@all"
        self.worker_key = str(worker_key or "").strip()
        self.worker_kind = str(worker_kind or "").strip()
        self.store_id = str(store_id or "").strip()
        self.store_name = str(store_name or "").strip()
        self.state_dir = Path(state_dir)
        self.retry_sec = max(10.0, float(retry_sec))
        self.machine_name = str(machine_name or "").strip() or socket.gethostname()

    @classmethod
    def from_env(
        cls,
        *,
        platform: str = "",
        store_id: str = "",
        store_name: str = "",
    ) -> "DecisionAlertNotifier":
        enabled_raw = _env_first("DECISION_ALERT_ENABLED", default="1").lower()
        enabled = enabled_raw not in {"0", "false", "no", "off"}
        decision_url = _env_first("DECISION_URL", default="http://127.0.0.1:18080")
        worker_key = _env_first("WORKER_KEY", default=f"{platform}_{store_id}".strip("_"))
        worker_kind = _env_first("WORKER_KIND", default=platform)
        state_dir = Path(
            _env_first("DECISION_ALERT_STATE_DIR", default=str(DEFAULT_STATE_DIR))
        ).expanduser()
        return cls(
            enabled=enabled,
            base_url=_env_first("WATCHDOG_NOTIFY_BASE_URL", default=decision_url),
            api_key=_env_first("WATCHDOG_NOTIFY_API_KEY", "CLAWBOT_TOOL_API_KEY"),
            app_key=_env_first("WATCHDOG_NOTIFY_APP_KEY", default="default"),
            to_user=_env_first("WATCHDOG_NOTIFY_TOUSER", default="@all"),
            worker_key=worker_key,
            worker_kind=worker_kind or platform,
            store_id=_env_first("STORE_ID", default=store_id),
            store_name=_env_first("STORE_NAME", default=store_name or store_id),
            state_dir=state_dir,
            retry_sec=float(_env_first("DECISION_ALERT_RETRY_SEC", default="300") or "300"),
            machine_name=_env_first("WATCHDOG_NOTIFY_MACHINE"),
        )

    def _state_path(self) -> Path:
        key = self.worker_key or f"{self.worker_kind}_{self.store_id}".strip("_") or "worker"
        return self.state_dir / f"{key}.json"

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "schema_version": 1,
            "phase": str(payload.get("phase") or PHASE_NORMAL),
            "issue_detail": str(payload.get("issue_detail") or ""),
            "issue_since_ms": int(payload.get("issue_since_ms") or 0),
            "issue_notified_at_ms": int(payload.get("issue_notified_at_ms") or 0),
            "recovery_detail": str(payload.get("recovery_detail") or ""),
            "last_attempt_at_ms": int(payload.get("last_attempt_at_ms") or 0),
        }

    def _save_state(self, state: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    def _should_retry(self, state: dict[str, Any], *, ts_ms: int) -> bool:
        last_attempt = int(state.get("last_attempt_at_ms") or 0)
        return ts_ms - last_attempt >= int(self.retry_sec * 1000)

    def _post_tool(self, *, content: str) -> tuple[bool, str]:
        if not self.enabled:
            return False, "notify_disabled"
        if not self.base_url:
            return False, "notify_base_url_missing"
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
        detail: str,
        issue_since_ms: int,
        session_key: str,
        platform_nickname: str,
        message_id: str,
        message_type: str,
    ) -> str:
        lines = [
            "⚠️【客服决策异常】",
            f"渠道：{_display_platform(self.worker_kind)}",
        ]
        if self.store_name:
            lines.append(f"店铺：{self.store_name}")
        if self.worker_key:
            lines.append(f"实例：{self.worker_key}")
        if platform_nickname:
            lines.append(f"客户：{_safe_text(platform_nickname, limit=80)}")
        if session_key:
            lines.append(f"会话：{_safe_text(session_key, limit=120)}")
        if message_type:
            lines.append(f"消息类型：{_safe_text(message_type, limit=40)}")
        if message_id:
            lines.append(f"消息ID：{_safe_text(message_id, limit=80)}")
        lines.extend(
            [
                "",
                f"持续：{_format_duration(now_ms() - issue_since_ms)}",
                f"主机：{self.machine_name}",
                "",
                "详情：",
                _safe_text(detail),
                "",
                "说明：异常未恢复会定时重复提醒。",
            ]
        )
        return "\n".join(lines).strip()

    def _build_recovery_message(self, *, issue_detail: str, issue_since_ms: int, ready_detail: str) -> str:
        lines = [
            "✅【客服决策恢复】",
            f"渠道：{_display_platform(self.worker_kind)}",
        ]
        if self.store_name:
            lines.append(f"店铺：{self.store_name}")
        if self.worker_key:
            lines.append(f"实例：{self.worker_key}")
        lines.extend(
            [
                "",
                f"异常持续：{_format_duration(now_ms() - issue_since_ms)}",
                f"主机：{self.machine_name}",
                f"恢复状态：{_safe_text(ready_detail, limit=160) or 'decision_api_ok'}",
                "",
                "原问题：",
                _safe_text(issue_detail),
            ]
        )
        return "\n".join(lines).strip()

    def record_failure(
        self,
        *,
        detail: str,
        session_key: str = "",
        platform_nickname: str = "",
        message_id: str = "",
        message_type: str = "",
    ) -> tuple[bool, str]:
        state = self._load_state()
        ts_ms = now_ms()
        state["issue_detail"] = _safe_text(detail)
        if str(state.get("phase") or PHASE_NORMAL) != PHASE_ISSUE:
            state["phase"] = PHASE_ISSUE
            state["issue_since_ms"] = ts_ms
            state["issue_notified_at_ms"] = 0
            state["recovery_detail"] = ""
            state["last_attempt_at_ms"] = 0

        if not self._should_retry(state, ts_ms=ts_ms):
            self._save_state(state)
            if int(state.get("issue_notified_at_ms") or 0) > 0:
                return False, "already_notified"
            return False, "retry_later"

        state["last_attempt_at_ms"] = ts_ms
        ok, result = self._post_tool(
            content=self._build_issue_message(
                detail=state["issue_detail"],
                issue_since_ms=int(state.get("issue_since_ms") or ts_ms),
                session_key=session_key,
                platform_nickname=platform_nickname,
                message_id=message_id,
                message_type=message_type,
            )
        )
        if ok:
            state["issue_notified_at_ms"] = ts_ms
        self._save_state(state)
        return ok, result

    def record_recovery(self, *, detail: str = "decision_api_ok") -> tuple[bool, str]:
        state = self._load_state()
        phase = str(state.get("phase") or PHASE_NORMAL)
        if phase == PHASE_NORMAL:
            return False, "normal"

        if int(state.get("issue_notified_at_ms") or 0) <= 0:
            state.update(
                {
                    "phase": PHASE_NORMAL,
                    "issue_detail": "",
                    "issue_since_ms": 0,
                    "issue_notified_at_ms": 0,
                    "recovery_detail": "",
                    "last_attempt_at_ms": 0,
                }
            )
            self._save_state(state)
            return False, "issue_not_notified"

        if phase != PHASE_RECOVERING:
            state["last_attempt_at_ms"] = 0
        state["phase"] = PHASE_RECOVERING
        state["recovery_detail"] = _safe_text(detail, limit=160)
        ts_ms = now_ms()
        if not self._should_retry(state, ts_ms=ts_ms):
            self._save_state(state)
            return False, "retry_later"

        state["last_attempt_at_ms"] = ts_ms
        ok, result = self._post_tool(
            content=self._build_recovery_message(
                issue_detail=str(state.get("issue_detail") or ""),
                issue_since_ms=int(state.get("issue_since_ms") or ts_ms),
                ready_detail=str(state.get("recovery_detail") or detail),
            )
        )
        if ok:
            state = {
                "schema_version": 1,
                "phase": PHASE_NORMAL,
                "issue_detail": "",
                "issue_since_ms": 0,
                "issue_notified_at_ms": 0,
                "recovery_detail": "",
                "last_attempt_at_ms": 0,
            }
        self._save_state(state)
        return ok, result


def build_notifier_for_message(
    *,
    platform: str,
    store_id: str,
    store_name: str,
) -> Optional[DecisionAlertNotifier]:
    notifier = DecisionAlertNotifier.from_env(
        platform=platform,
        store_id=store_id,
        store_name=store_name,
    )
    if not notifier.worker_key:
        return None
    return notifier
