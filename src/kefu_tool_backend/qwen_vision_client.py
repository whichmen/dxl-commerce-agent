from __future__ import annotations

import base64
import json
import mimetypes
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class VisionCallResult:
    ok: bool
    message: str
    data: dict[str, Any]


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        return {}
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _normalize_image_input(image_ref: str) -> str:
    value = (image_ref or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://", "data:")):
        return value
    if value.startswith("file://"):
        value = value[len("file://") :]
    path = Path(value)
    if not path.exists() or (not path.is_file()):
        return image_ref
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _safe_float(raw: Any, default: float) -> float:
    try:
        return float(raw)
    except Exception:
        return float(default)


class QwenVisionClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "qwen3-vl-plus",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        timeout_sec: float = 15.0,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or "qwen3-vl-plus"
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout_sec = max(2.0, _safe_float(timeout_sec, default=15.0))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url)

    def inspect(
        self,
        *,
        image_url: str,
        task: str,
        customer_text: str,
        reference_image_url: str = "",
    ) -> VisionCallResult:
        if not self.enabled:
            return VisionCallResult(
                ok=False,
                message="vision_not_configured",
                data={},
            )
        image_norm = _normalize_image_input(image_url)
        if not image_norm:
            return VisionCallResult(
                ok=False,
                message="missing_image_url",
                data={},
            )
        if image_norm.startswith("blob:"):
            return VisionCallResult(
                ok=False,
                message="unsupported_image_url_scheme:blob",
                data={},
            )
        reference_norm = _normalize_image_input(reference_image_url)
        if reference_norm.startswith("blob:"):
            reference_norm = ""

        prompt = self._build_prompt(
            task=task,
            customer_text=customer_text,
            has_reference=bool(reference_norm),
        )
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image_norm}},
        ]
        if reference_norm:
            content.append({"type": "image_url", "image_url": {"url": reference_norm}})

        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "temperature": 0.1,
        }
        endpoint = f"{self.base_url}/chat/completions"

        try:
            payload = self._post_json(endpoint=endpoint, payload=body)
        except Exception as exc:
            return VisionCallResult(
                ok=False,
                message=f"vision_call_failed:{type(exc).__name__}",
                data={},
            )

        answer_text = self._extract_text(payload)
        parsed = _extract_json_object(answer_text)
        return VisionCallResult(
            ok=True,
            message="ok",
            data={
                "model": self.model,
                "task": task,
                "answer_text": answer_text,
                "parsed": parsed,
            },
        )

    def extract_text(
        self,
        *,
        image_url: str,
    ) -> VisionCallResult:
        if not self.enabled:
            return VisionCallResult(
                ok=False,
                message="vision_not_configured",
                data={},
            )
        image_norm = _normalize_image_input(image_url)
        if not image_norm:
            return VisionCallResult(
                ok=False,
                message="missing_image_url",
                data={},
            )
        if image_norm.startswith("blob:"):
            return VisionCallResult(
                ok=False,
                message="unsupported_image_url_scheme:blob",
                data={},
            )

        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self._build_general_ocr_prompt()},
                        {"type": "image_url", "image_url": {"url": image_norm}},
                    ],
                }
            ],
            "temperature": 0.0,
        }
        endpoint = f"{self.base_url}/chat/completions"

        try:
            payload = self._post_json(endpoint=endpoint, payload=body)
        except Exception as exc:
            return VisionCallResult(
                ok=False,
                message=f"vision_call_failed:{type(exc).__name__}",
                data={},
            )

        answer_text = self._extract_text(payload)
        parsed = _extract_json_object(answer_text)
        text = ""
        if isinstance(parsed, dict):
            raw_text = parsed.get("text")
            if raw_text is not None:
                text = str(raw_text).strip()
        if not text:
            text = answer_text.strip()

        return VisionCallResult(
            ok=True,
            message="ok",
            data={
                "model": self.model,
                "task": "ocr_text",
                "text": text,
                "answer_text": answer_text,
                "parsed": parsed,
            },
        )

    def _build_prompt(self, *, task: str, customer_text: str, has_reference: bool) -> str:
        text = (customer_text or "").strip()
        task_norm = (task or "").strip().lower()

        if task_norm == "same_item_compare" and has_reference:
            return (
                "你是电商质检助手。你将看到两张图片：第一张是顾客图片，第二张是参考商品图。\n"
                "请判断是否同款/同类衣服，并检查顾客图中是否有明显破损。\n"
                "只输出JSON："
                '{"is_same_item":"yes|no|uncertain","same_confidence":0.0,'
                '"damage_detected":"yes|no|uncertain","damage_types":["..."],'
                '"severity":"low|medium|high|uncertain","evidence":"简短中文"}\n'
                f"顾客文字描述：{text or '（无）'}"
            )

        if task_norm in {"ocr_code", "check_clothing_code_and_quality"}:
            return (
                "你是电商图片识别助手。请从图片中识别衣服编码/吊牌文字，并给出是否看清、是否有明显质量问题。\n"
                "只输出JSON："
                '{"is_clothing":"yes|no|uncertain","code_visible":"yes|no|uncertain",'
                '"clothing_code":"",'
                '"extracted_text":["..."],'
                '"damage_detected":"yes|no|uncertain",'
                '"damage_types":["..."],'
                '"confidence":0.0,'
                '"evidence":"简短中文"}\n'
                f"顾客文字描述：{text or '（无）'}"
            )

        return (
            "你是电商质检助手。请根据顾客图片判断衣物是否有明显质量问题（破洞、开线、污渍、拉链/扣子损坏等）。\n"
            "只输出JSON："
            '{"damage_detected":"yes|no|uncertain","damage_types":["..."],'
            '"severity":"low|medium|high|uncertain","confidence":0.0,'
            '"is_clothing":"yes|no|uncertain","item_guess":"",'
            '"evidence":"简短中文"}\n'
            f"顾客文字描述：{text or '（无）'}"
        )

    def _build_general_ocr_prompt(self) -> str:
        return (
            "你是OCR助手。请按正常阅读顺序识别图片中的可见文字。\n"
            "只输出JSON：{\"text\":\"...\"}\n"
            "要求：\n"
            "1. 不要解释，不要补充说明。\n"
            "2. 多行文字用换行符连接。\n"
            "3. 识别不到文字时，text 设为空字符串。"
        )

    def _extract_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return ""
        if not choices:
            return ""
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if text:
                    parts.append(text)
            return "\n".join(parts).strip()
        return ""

    def _post_json(self, *, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url=endpoint, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"vision_http_{exc.code}:{detail[:220]}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("vision_timeout") from exc
            raise
        except TimeoutError:
            raise

        if not raw:
            return {}
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
