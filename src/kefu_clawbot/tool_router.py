from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import IncomingEvent


SUPPORTED_TOOLS = {
    "order_lookup",
    "logistics_lookup",
    "approve_refund_by_order_id",
    "refund_history_lookup",
    "batch_reclass",
    "other_actionable_review",
    "file_read",
    "shell_exec",
}

ADMIN_ONLY_TOOLS = {
    "batch_reclass",
    "other_actionable_review",
}


@dataclass
class ToolResult:
    ok: bool
    tool_name: str
    data: dict[str, Any]
    message: str
    source: str


class ToolRouter:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        timeout_sec: float = 8.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_sec = max(1.0, float(timeout_sec))

    def execute(
        self,
        *,
        tool_name: str,
        tool_args: dict[str, Any],
        event: IncomingEvent,
    ) -> ToolResult:
        normalized = (tool_name or "").strip()
        if normalized not in SUPPORTED_TOOLS:
            return ToolResult(
                ok=False,
                tool_name=normalized or "none",
                data={},
                message="unsupported_tool",
                source="router",
            )
        if normalized in ADMIN_ONLY_TOOLS and event.role_key() != "admin":
            return ToolResult(
                ok=False,
                tool_name=normalized,
                data={},
                message="tool_forbidden_for_role",
                source="router",
            )
        if not self.base_url:
            return ToolResult(
                ok=False,
                tool_name=normalized,
                data={},
                message="tool_backend_not_configured",
                source="router",
            )

        payload = {
            "tool_name": normalized,
            "args": tool_args,
            "event": event.model_dump(),
        }
        endpoint = f"{self.base_url}/v1/tools/{normalized}"
        try:
            data = self._post_json(endpoint, payload)
        except Exception as exc:
            return ToolResult(
                ok=False,
                tool_name=normalized,
                data={},
                message=f"tool_call_failed:{type(exc).__name__}",
                source="router",
            )

        ok = bool(data.get("ok", False))
        result_data = data.get("data")
        if not isinstance(result_data, dict):
            result_data = {}
        message = str(data.get("message") or ("ok" if ok else "tool_backend_error"))
        return ToolResult(
            ok=ok,
            tool_name=normalized,
            data=result_data,
            message=message,
            source="http",
        )

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        if self.api_key:
            req.add_header("X-Api-Key", self.api_key)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("tool_api_timeout") from exc
            raise
        except TimeoutError:
            raise

        if not raw:
            return {}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
