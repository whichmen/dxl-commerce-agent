from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from .qwen_vision_client import (
    VisionCallResult,
    _extract_json_object,
    _normalize_image_input,
    _safe_float,
)


class HermesOcrClient:
    def __init__(
        self,
        *,
        url: str,
        route: str = "gpt55",
        model: str = "gpt-5.3-codex-spark",
        api_key: str = "",
        timeout_sec: float = 60.0,
        client_id: str = "kefu-worker-ocr",
    ) -> None:
        self.url = (url or "").strip()
        self.route = (route or "").strip() or "gpt55"
        self.model = (model or "").strip() or "gpt-5.3-codex-spark"
        self.api_key = (api_key or "").strip()
        self.timeout_sec = max(2.0, _safe_float(timeout_sec, default=60.0))
        self.client_id = (client_id or "").strip() or "kefu-worker-ocr"

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def extract_text(self, *, image_url: str) -> VisionCallResult:
        if not self.enabled:
            return VisionCallResult(ok=False, message="ocr_not_configured", data={})

        image_norm = _normalize_image_input(image_url)
        if not image_norm:
            return VisionCallResult(ok=False, message="missing_image_url", data={})
        if not image_norm.startswith("data:image/"):
            return VisionCallResult(ok=False, message="unsupported_image_url", data={})

        body = {
            "client_id": self.client_id,
            "task": "kefu_worker_ocr",
            "route": self.route,
            "model": self.model,
            "system": "你是OCR助手。只识别图片中的可见文字，不做客服回复。",
            "prompt": self._build_prompt(),
            "images": [image_norm],
            "temperature": 0,
            "response_format": "json",
        }

        try:
            payload = self._post_json(payload=body)
        except Exception as exc:
            return VisionCallResult(
                ok=False,
                message=f"ocr_call_failed:{type(exc).__name__}",
                data={},
            )

        answer_text = str(payload.get("text") or "").strip()
        parsed = payload.get("json")
        if not isinstance(parsed, dict):
            parsed = _extract_json_object(answer_text)

        if isinstance(parsed, dict) and parsed.get("text") is not None:
            text = str(parsed.get("text") or "").strip()
        else:
            text = answer_text.strip()

        return VisionCallResult(
            ok=True,
            message="ok",
            data={
                "model": str(payload.get("response_model") or self.model),
                "task": "ocr_text",
                "text": text,
                "answer_text": answer_text,
                "parsed": parsed if isinstance(parsed, dict) else {},
            },
        )

    def _build_prompt(self) -> str:
        return (
            "请按正常阅读顺序识别图片里的可见文字。\n"
            "只输出JSON：{\"text\":\"...\"}\n"
            "要求：识别不到文字时 text 为空字符串；多行文字用换行符连接；不要解释。"
        )

    def _post_json(self, *, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"ocr_http_{exc.code}:{detail[:220]}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("ocr_timeout") from exc
            raise
        except TimeoutError:
            raise

        if not raw:
            return {}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
