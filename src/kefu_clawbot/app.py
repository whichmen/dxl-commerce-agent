from __future__ import annotations

import os
import uuid
import hashlib
import json
import io
import base64
import re
import mimetypes
import shutil
import subprocess
import time
from threading import Lock
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import __version__
from .classifier import classify_event
from .intercept_snapshot import build_intercept_snapshot
from .models import (
    AgentPromptRequest,
    CategoryResult,
    DecisionInternal,
    DecisionRequest,
    DecisionResponse,
    IncomingEvent,
    WorkerCaptureOcrBatchRequest,
    now_ms,
)
from .openclaw_agent import (
    DEFAULT_ATTACHMENT_MAX_BYTES,
    OpenClawAgentInvoker,
    build_gateway_attachments_for_event,
    looks_like_json_reply,
)
from .policy import PolicyEngine
from .storage import SQLiteStore
from .wecom_bridge import WeComBridge, WeComIncomingMessage
from kefu_tool_backend.hermes_ocr_client import HermesOcrClient
from kefu_tool_backend.kuaimai_client import KuaimaiClient
from kefu_tool_backend.qwen_vision_client import QwenVisionClient
from kefu_tool_backend.tool_backend_service import ToolBackendService

IMAGE_URL_MARKER_RE = re.compile(r"\[\[\s*IMAGE_URL\s*:\s*(https?://[^\]\s]+)\s*\]\]", re.IGNORECASE)
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*]\((https?://[^)\s]+)\)")


def _bundled_path(relative_path: str) -> str:
    relative = Path(relative_path)
    candidates = (
        Path.cwd() / relative,
        Path(__file__).resolve().parents[2] / relative,
        Path(__file__).resolve().parents[1] / relative,
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(relative)


class ServiceState:
    def __init__(
        self,
        *,
        policy: PolicyEngine,
        store: SQLiteStore,
        api_key: str,
        decision_mode: str,
        fallback_text: str,
        image_no_text_reply: str,
        agent_invoker: OpenClawAgentInvoker | None,
        tool_backend: ToolBackendService | None,
        worker_ocr_provider: str,
        worker_ocr_client: HermesOcrClient | None,
        tool_api_key: str,
        tool_base_url: str,
        identity_base_url: str,
        kuaimai_erp_host: str,
        kuaimai_browser_channel: str,
        history_days: int,
        wecom_bridge: WeComBridge | None,
        wecom_app_name: str,
        wecom_hub_url: str,
        wecom_hub_api_key: str,
        wecom_app_key: str,
        media_root: Path,
        media_fetch_timeout_sec: float,
        media_max_bytes: int,
        adb_capture_enabled: bool,
        adb_bin: str,
        adb_capture_timeout_sec: float,
    ) -> None:
        self.policy = policy
        self.store = store
        self.api_key = api_key
        self.decision_mode = decision_mode
        self.fallback_text = fallback_text
        self.image_no_text_reply = image_no_text_reply
        self.agent_invoker = agent_invoker
        self.tool_backend = tool_backend
        self.worker_ocr_provider = (worker_ocr_provider or "").strip().lower()
        self.worker_ocr_client = worker_ocr_client
        self.tool_api_key = tool_api_key
        self.tool_base_url = tool_base_url
        self.identity_base_url = identity_base_url
        self.kuaimai_erp_host = kuaimai_erp_host
        self.kuaimai_browser_channel = kuaimai_browser_channel
        self.history_days = max(1, min(int(history_days), 7))
        self.wecom_bridge = wecom_bridge
        self.wecom_app_name = wecom_app_name
        self.wecom_hub_url = str(wecom_hub_url or "").rstrip("/")
        self.wecom_hub_api_key = str(wecom_hub_api_key or "").strip()
        self.wecom_app_key = str(wecom_app_key or "").strip()
        self.media_root = Path(media_root)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.media_fetch_timeout_sec = max(2.0, float(media_fetch_timeout_sec))
        self.media_max_bytes = max(256 * 1024, int(media_max_bytes))
        self.adb_capture_enabled = bool(adb_capture_enabled)
        self.adb_bin = str(adb_bin or "").strip()
        self.adb_capture_timeout_sec = max(2.0, float(adb_capture_timeout_sec))
        self.worker_ocr_cache_ttl_sec = 600.0
        self.worker_ocr_cache_max_entries = 20000
        self.started_at_ms = now_ms()
        self._session_guard = Lock()
        self._session_locks: dict[str, Lock] = {}
        self._worker_ocr_cache_guard = Lock()
        self._worker_ocr_cache: dict[str, tuple[float, str]] = {}

    def get_session_lock(self, session_key: str) -> Lock:
        with self._session_guard:
            lock = self._session_locks.get(session_key)
            if lock is None:
                lock = Lock()
                self._session_locks[session_key] = lock
        return lock

    def get_wecom_session_lock(self, session_key: str) -> Lock:
        return self.get_session_lock(session_key)


def _resolve_media_path(args: dict[str, Any], event: dict[str, Any]) -> str:
    event_raw = event.get("raw")
    event_raw_map = event_raw if isinstance(event_raw, dict) else {}
    candidates = (
        args.get("media_path"),
        event.get("media_path"),
        event_raw_map.get("media_path"),
    )
    for item in candidates:
        value = str(item or "").strip()
        if not value:
            continue
        if value.startswith("file://"):
            value = value[7:]
        if Path(value).is_file():
            return str(Path(value).resolve())
    return ""


def _coerce_wecom_recipients(raw: Any) -> str:
    candidates: list[str] = []
    if isinstance(raw, (list, tuple, set)):
        raw_items = list(raw)
    else:
        text = str(raw or "").strip()
        if not text:
            return ""
        raw_items = re.split(r"[|,，;；\s]+", text)

    for item in raw_items:
        value = str(item or "").strip()
        if not value:
            continue
        if value == "@all":
            return "@all"
        if value not in candidates:
            candidates.append(value)
    return "|".join(candidates)


def _split_text_by_utf8_bytes(
    *,
    content: str,
    max_bytes: int,
    max_chunks: int,
) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return [text]

    chunks: list[str] = []
    remain = data
    while remain and len(chunks) < max_chunks:
        if len(remain) <= max_bytes:
            piece = remain.decode("utf-8", errors="ignore").strip()
            if piece:
                chunks.append(piece)
            remain = b""
            break

        cut = remain[:max_bytes]
        while cut:
            try:
                cut.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                cut = cut[: exc.start]
        if not cut:
            break
        piece = cut.decode("utf-8", errors="ignore").strip()
        if piece:
            chunks.append(piece)
        remain = remain[len(cut) :].lstrip()

    if remain:
        tail = remain.decode("utf-8", errors="ignore").strip()
        if tail:
            if chunks:
                chunks[-1] = f"{chunks[-1]}\n……{tail[:40]}"
            else:
                chunks.append(f"{tail[:80]}……")
    return chunks


def _invoke_wecom_notify_tool(
    *,
    service: ServiceState,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if service.wecom_hub_url:
        return _invoke_wecom_notify_via_hub(service=service, payload=payload)

    bridge = service.wecom_bridge
    if bridge is None:
        return {"ok": False, "message": "wecom_not_enabled", "data": {}}

    payload_obj = payload if isinstance(payload, dict) else {}
    args_obj = payload_obj.get("args")
    event_obj = payload_obj.get("event")
    args = dict(args_obj) if isinstance(args_obj, dict) else {}
    event = dict(event_obj) if isinstance(event_obj, dict) else {}
    raw_event = event.get("raw")
    raw_event_map = dict(raw_event) if isinstance(raw_event, dict) else {}

    to_user = ""
    for candidate in (
        args.get("touser"),
        args.get("to_user"),
        args.get("user_id"),
        args.get("userid"),
        event.get("touser"),
        event.get("to_user"),
        raw_event_map.get("touser"),
        raw_event_map.get("to_user"),
        os.getenv("CLAWBOT_WECOM_DEFAULT_TOUSER", ""),
    ):
        to_user = _coerce_wecom_recipients(candidate)
        if to_user:
            break
    if not to_user:
        return {"ok": False, "message": "missing_touser", "data": {}}

    msg_type = str(args.get("msg_type") or args.get("message_type") or "text").strip().lower()
    if msg_type not in {"text", "image"}:
        msg_type = "text"

    if msg_type == "image":
        image_url = str(
            args.get("image_url")
            or args.get("media_url")
            or event.get("media_url")
            or raw_event_map.get("media_url")
            or ""
        ).strip()
        if not image_url:
            return {
                "ok": False,
                "message": "missing_image_url",
                "data": {"touser": to_user, "msg_type": "image"},
            }
        try:
            send_ret = bridge.send_image_url(to_user=to_user, image_url=image_url)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"wecom_send_exception:{type(exc).__name__}",
                "data": {"touser": to_user, "msg_type": "image", "image_url": image_url},
            }
        ok = bool(send_ret.get("ok"))
        if ok:
            service.store.record_wecom_send(to_user)
        return {
            "ok": ok,
            "message": str(send_ret.get("message") or ("ok" if ok else "wecom_send_image_failed")),
            "data": {
                "touser": to_user,
                "msg_type": "image",
                "image_url": image_url,
                "send_result": send_ret.get("data") if isinstance(send_ret.get("data"), dict) else {},
            },
        }

    content = str(
        args.get("content")
        or args.get("text")
        or args.get("message")
        or event.get("text")
        or ""
    ).strip()
    if not content:
        return {
            "ok": False,
            "message": "missing_content",
            "data": {"touser": to_user, "msg_type": msg_type},
        }

    max_bytes = _safe_int(args.get("max_bytes"), default=1800)
    max_chunks = _safe_int(args.get("max_chunks"), default=8)
    max_bytes = max(200, min(max_bytes, 3000))
    max_chunks = max(1, min(max_chunks, 20))
    chunks = _split_text_by_utf8_bytes(content=content, max_bytes=max_bytes, max_chunks=max_chunks)
    if not chunks:
        return {
            "ok": False,
            "message": "missing_content",
            "data": {"touser": to_user, "msg_type": msg_type},
        }

    sent_chunks: list[str] = []
    last_data: dict[str, Any] = {}
    for idx, chunk in enumerate(chunks, start=1):
        try:
            send_ret = bridge.send_text(to_user=to_user, content=chunk)
        except Exception as exc:
            return {
                "ok": False,
                "message": f"wecom_send_exception:{type(exc).__name__}",
                "data": {
                    "touser": to_user,
                    "msg_type": msg_type,
                    "sent_chunks": len(sent_chunks),
                    "total_chunks": len(chunks),
                    "failed_chunk_index": idx,
                },
            }
        if not bool(send_ret.get("ok")):
            return {
                "ok": False,
                "message": str(send_ret.get("message") or "wecom_send_text_failed"),
                "data": {
                    "touser": to_user,
                    "msg_type": msg_type,
                    "sent_chunks": len(sent_chunks),
                    "total_chunks": len(chunks),
                    "failed_chunk_index": idx,
                    "send_result": send_ret.get("data") if isinstance(send_ret.get("data"), dict) else {},
                },
            }
        service.store.record_wecom_send(to_user)
        sent_chunks.append(chunk)
        last_data = send_ret.get("data") if isinstance(send_ret.get("data"), dict) else {}

    return {
        "ok": True,
        "message": "ok",
        "data": {
            "touser": to_user,
            "msg_type": msg_type,
            "sent_chunks": len(sent_chunks),
            "total_chunks": len(chunks),
            "sent_text": "\n".join(sent_chunks).strip(),
            "send_result": last_data,
        },
    }


def _http_request(
    *,
    method: str,
    url: str,
    timeout_sec: float,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes]:
    req = urllib_request.Request(url=url, data=body, method=method.upper())
    for key, value in (headers or {}).items():
        if value:
            req.add_header(key, value)
    try:
        with urllib_request.urlopen(req, timeout=timeout_sec) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except urllib_error.HTTPError as exc:
        return int(exc.code), exc.read()


def _build_wecom_hub_callback_url(service: ServiceState, request: Request) -> str:
    base = service.wecom_hub_url.rstrip("/")
    app_key = urllib_parse.quote(service.wecom_app_key, safe="")
    url = f"{base}/v1/wecom/{app_key}/callback"
    query = request.url.query
    if query:
        url = f"{url}?{query}"
    return url


def _proxy_wecom_verify(service: ServiceState, request: Request) -> PlainTextResponse:
    if not service.wecom_hub_url or not service.wecom_app_key:
        raise HTTPException(status_code=404, detail="wecom_not_enabled")
    status_code, body = _http_request(
        method="GET",
        url=_build_wecom_hub_callback_url(service, request),
        timeout_sec=8.0,
    )
    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=body.decode("utf-8", errors="ignore")[:200])
    return PlainTextResponse(body.decode("utf-8", errors="ignore"))


def _proxy_wecom_callback(service: ServiceState, request: Request, body: bytes) -> PlainTextResponse:
    if not service.wecom_hub_url or not service.wecom_app_key:
        raise HTTPException(status_code=404, detail="wecom_not_enabled")
    content_type = str(request.headers.get("content-type") or "").strip()
    status_code, resp_body = _http_request(
        method="POST",
        url=_build_wecom_hub_callback_url(service, request),
        timeout_sec=8.0,
        headers={"Content-Type": content_type or "application/xml; charset=utf-8"},
        body=body,
    )
    if status_code >= 400:
        raise HTTPException(status_code=status_code, detail=resp_body.decode("utf-8", errors="ignore")[:200])
    return PlainTextResponse(resp_body.decode("utf-8", errors="ignore") or "success")


def _invoke_wecom_notify_via_hub(
    *,
    service: ServiceState,
    payload: dict[str, Any],
) -> dict[str, Any]:
    hub_url = service.wecom_hub_url.rstrip("/")
    if not hub_url:
        return {"ok": False, "message": "wecom_hub_not_configured", "data": {}}

    payload_obj = payload if isinstance(payload, dict) else {}
    args_obj = payload_obj.get("args")
    args = dict(args_obj) if isinstance(args_obj, dict) else {}
    event_obj = payload_obj.get("event")
    event = dict(event_obj) if isinstance(event_obj, dict) else {}
    app_key = str(args.get("app_key") or "").strip()
    if app_key.lower() in {"", "default"}:
        args["app_key"] = service.wecom_app_key

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    if service.wecom_hub_api_key:
        headers["X-Api-Key"] = service.wecom_hub_api_key

    status_code, body = _http_request(
        method="POST",
        url=f"{hub_url}/v1/wecom/send",
        timeout_sec=8.0,
        headers=headers,
        body=json.dumps({"args": args, "event": event}, ensure_ascii=False).encode("utf-8"),
    )
    text = body.decode("utf-8", errors="ignore")
    try:
        parsed = json.loads(text or "{}")
    except Exception:
        parsed = {}
    if status_code >= 400:
        return {
            "ok": False,
            "message": f"wecom_hub_http_{status_code}",
            "data": parsed if isinstance(parsed, dict) else {},
        }
    return parsed if isinstance(parsed, dict) else {"ok": False, "message": "wecom_hub_invalid_json", "data": {}}


def _invoke_tool(
    *,
    service: ServiceState,
    tool_name: str,
    payload: dict[str, Any],
    timeout_sec: float = 8.0,
) -> dict[str, Any]:
    payload_obj = payload if isinstance(payload, dict) else {}
    if tool_name == "wecom_notify":
        return _invoke_wecom_notify_tool(
            service=service,
            payload=payload_obj,
        )
    backend = service.tool_backend
    if backend is None:
        return {"ok": False, "message": "tool_backend_not_configured", "data": {}}
    args_obj = payload_obj.get("args")
    event_obj = payload_obj.get("event")
    args = dict(args_obj) if isinstance(args_obj, dict) else {}
    event = dict(event_obj) if isinstance(event_obj, dict) else {}

    payload_obj = {"args": args, "event": event}

    result = backend.handle(tool_name=tool_name, payload=payload_obj)
    return result.as_dict()


def _safe_image_suffix(*, source_ref: str, content_type: str = "") -> str:
    suffix = Path(urllib_parse.urlparse(source_ref).path or "").suffix.lower()
    if not suffix and content_type:
        suffix = (mimetypes.guess_extension(content_type.split(";")[0].strip().lower()) or "").lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
        return suffix
    return ".jpg"


def _persist_uploaded_media(
    service: ServiceState,
    *,
    content: bytes,
    mime_type: str = "",
    source_ref: str = "",
) -> dict[str, Any] | None:
    if not content:
        return None
    total = len(content)
    if total <= 0 or total > service.media_max_bytes:
        return None
    digest = hashlib.sha1(content).hexdigest()
    media_id = f"media_{digest[:24]}"
    source = str(source_ref or "").strip() or f"upload:{digest}"
    suffix = _safe_image_suffix(source_ref=source, content_type=str(mime_type or ""))
    final_path = service.media_root / f"{media_id}{suffix}"
    if not final_path.exists():
        final_path.write_bytes(content)
    resolved = str(final_path.resolve())
    service.store.upsert_media_asset(
        media_id=media_id,
        source_ref=source,
        local_path=resolved,
        sha1=digest,
        size_bytes=total,
    )
    return {
        "media_path": resolved,
        "sha1": digest,
        "size_bytes": total,
    }


def _materialize_media_ref(service: ServiceState, source_ref: str) -> dict[str, Any] | None:
    ref = str(source_ref or "").strip()
    if not ref:
        return None

    cached = service.store.get_media_asset_by_source_ref(ref)
    if isinstance(cached, dict):
        local_path = str(cached.get("local_path") or "").strip()
        if local_path and Path(local_path).exists():
            return cached

    tmp_path = service.media_root / f"tmp_{uuid.uuid4().hex}.bin"
    sha1 = hashlib.sha1()
    total = 0
    content_type = ""
    try:
        if ref.startswith(("http://", "https://")):
            req = urllib_request.Request(
                url=ref,
                headers={"User-Agent": "Mozilla/5.0"},
                method="GET",
            )
            with urllib_request.urlopen(req, timeout=service.media_fetch_timeout_sec) as resp:
                content_type = str(resp.headers.get("Content-Type") or "").strip()
                with tmp_path.open("wb") as fp:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > service.media_max_bytes:
                            return None
                        sha1.update(chunk)
                        fp.write(chunk)
        else:
            local = Path(ref[7:]) if ref.startswith("file://") else Path(ref)
            if not local.exists() or (not local.is_file()):
                return None
            local = local.resolve()
            content_type = str(mimetypes.guess_type(str(local))[0] or "").strip()
            with local.open("rb") as src:
                while True:
                    chunk = src.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > service.media_max_bytes:
                        return None
                    sha1.update(chunk)
            if total <= 0:
                return None

            digest = sha1.hexdigest()
            media_id = f"media_{digest[:24]}"
            service.store.upsert_media_asset(
                media_id=media_id,
                source_ref=ref,
                local_path=str(local),
                sha1=digest,
                size_bytes=total,
            )
            return {
                "media_id": media_id,
                "source_ref": ref,
                "local_path": str(local),
                "sha1": digest,
                "size_bytes": total,
            }
        if total <= 0:
            return None

        digest = sha1.hexdigest()
        media_id = f"media_{digest[:24]}"
        suffix = _safe_image_suffix(source_ref=ref, content_type=content_type)
        final_path = service.media_root / f"{media_id}{suffix}"
        if not final_path.exists():
            shutil.move(str(tmp_path), str(final_path))
        else:
            tmp_path.unlink(missing_ok=True)
        resolved = str(final_path.resolve())

        service.store.upsert_media_asset(
            media_id=media_id,
            source_ref=ref,
            local_path=resolved,
            sha1=digest,
            size_bytes=total,
        )
        return {
            "media_id": media_id,
            "source_ref": ref,
            "local_path": resolved,
            "sha1": digest,
            "size_bytes": total,
        }
    except Exception:
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def _materialize_event_media(service: ServiceState, event: IncomingEvent) -> IncomingEvent:
    if str(event.message_type or "").strip().lower() != "image":
        return event
    raw = dict(event.raw or {})

    raw_media_path = str(raw.get("media_path") or "").strip()
    if raw_media_path:
        local = Path(raw_media_path[7:]) if raw_media_path.startswith("file://") else Path(raw_media_path)
        if local.exists() and local.is_file():
            raw["media_path"] = str(local.resolve())
            return event.model_copy(update={"raw": raw})

    raw_media_sha1 = str(raw.get("media_sha1") or "").strip().lower()
    if raw_media_sha1:
        asset = service.store.get_media_asset_by_sha1(raw_media_sha1)
        if isinstance(asset, dict):
            local_path = str(asset.get("local_path") or "").strip()
            if local_path and Path(local_path).exists():
                raw["media_path"] = str(Path(local_path).resolve())
                return event.model_copy(update={"raw": raw})

    source_ref = str(event.media_url or "").strip()
    if source_ref:
        local = Path(source_ref[7:]) if source_ref.startswith("file://") else Path(source_ref)
        if local.exists() and local.is_file():
            raw["media_path"] = str(local.resolve())
            return event.model_copy(update={"raw": raw})

    if source_ref:
        materialized = _materialize_media_ref(service, source_ref)
        if isinstance(materialized, dict):
            media_path = str(materialized.get("local_path") or "").strip()
            if media_path:
                raw["media_path"] = media_path
                return event.model_copy(update={"raw": raw})

    adb_media_path = _capture_image_via_adb(service=service, event=event, raw=raw)
    if adb_media_path:
        raw["media_path"] = adb_media_path
        return event.model_copy(update={"raw": raw})

    return event


def _resolve_adb_device_serial(raw: dict[str, Any]) -> str:
    for key in ("device_serial", "adb_device_serial", "adb_serial"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def _run_adb(
    *,
    adb_exe: Path,
    serial: str,
    args: list[str],
    timeout_sec: float,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    cmd = [str(adb_exe), "-s", serial, *args]
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        timeout=timeout_sec,
        check=False,
    )


def _parse_bounds(bounds_sig: str) -> tuple[int, int, int, int] | None:
    parts = [p.strip() for p in str(bounds_sig or "").split(",")]
    if len(parts) != 4:
        return None
    try:
        x1, y1, x2, y2 = [int(v) for v in parts]
    except Exception:
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _crop_png_bytes(png_data: bytes, bounds: tuple[int, int, int, int]) -> bytes:
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return b""
    try:
        with Image.open(io.BytesIO(png_data)) as img:
            width, height = img.size
            x1, y1, x2, y2 = bounds
            x1 = max(0, min(x1, width - 1))
            y1 = max(0, min(y1, height - 1))
            x2 = max(x1 + 1, min(x2, width))
            y2 = max(y1 + 1, min(y2, height))
            if x2 <= x1 or y2 <= y1:
                return b""
            cropped = img.crop((x1, y1, x2, y2))
            out = io.BytesIO()
            cropped.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return b""


def _png_bytes_to_data_url(png_data: bytes) -> str:
    if not png_data:
        return ""
    b64 = base64.b64encode(png_data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _dump_ui_xml(*, adb_exe: Path, serial: str, timeout_sec: float) -> str:
    dump = _run_adb(
        adb_exe=adb_exe,
        serial=serial,
        args=["shell", "uiautomator", "dump", "/sdcard/__clawbot_ui.xml"],
        timeout_sec=timeout_sec,
        capture_output=True,
    )
    if dump.returncode != 0:
        return ""
    cat = _run_adb(
        adb_exe=adb_exe,
        serial=serial,
        args=["shell", "cat", "/sdcard/__clawbot_ui.xml"],
        timeout_sec=timeout_sec,
        capture_output=True,
    )
    if cat.returncode != 0:
        return ""
    xml = (cat.stdout or b"").decode("utf-8", errors="ignore")
    if "<hierarchy" not in xml:
        return ""
    return xml


def _xml_has_chat_input(xml: str) -> bool:
    if not xml:
        return False
    return 'class="android.widget.EditText"' in xml


def _capture_image_via_adb(*, service: ServiceState, event: IncomingEvent, raw: dict[str, Any]) -> str:
    if not service.adb_capture_enabled:
        return ""
    capture_mode = str(raw.get("image_capture_mode") or "").strip().lower()
    image_bounds = str(raw.get("image_bounds") or "").strip()
    if not image_bounds:
        return ""
    adb_bin = str(service.adb_bin or "").strip()
    if not adb_bin:
        return ""
    adb_exe = Path(adb_bin)
    if not adb_exe.exists():
        return ""
    serial = _resolve_adb_device_serial(raw)
    if not serial:
        return ""

    bounds = _parse_bounds(image_bounds)
    if capture_mode == "chat_bounds_crop" and bounds is None:
        return ""

    opened_preview = False
    if capture_mode in {"worker_preview_gateway_readonly", "chat_bounds_crop"}:
        opened_preview = False
    elif bounds is not None:
        x1, y1, x2, y2 = bounds
        tap_x = (x1 + x2) // 2
        tap_y = (y1 + y2) // 2
        try:
            before_xml = _dump_ui_xml(adb_exe=adb_exe, serial=serial, timeout_sec=service.adb_capture_timeout_sec)
            before_has_input = _xml_has_chat_input(before_xml)
            _run_adb(
                adb_exe=adb_exe,
                serial=serial,
                args=["shell", "input", "tap", str(tap_x), str(tap_y)],
                timeout_sec=service.adb_capture_timeout_sec,
                capture_output=False,
            )
            time.sleep(1.0)
            after_xml = _dump_ui_xml(adb_exe=adb_exe, serial=serial, timeout_sec=service.adb_capture_timeout_sec)
            after_has_input = _xml_has_chat_input(after_xml)
            opened_preview = bool(before_has_input and not after_has_input)
        except Exception:
            opened_preview = False

    try:
        proc = _run_adb(
            adb_exe=adb_exe,
            serial=serial,
            args=["exec-out", "screencap", "-p"],
            timeout_sec=service.adb_capture_timeout_sec,
            capture_output=True,
        )
    finally:
        if opened_preview and capture_mode not in {"worker_preview_gateway_readonly", "chat_bounds_crop"}:
            try:
                # 千帆全屏图页里 BACK 常无效，优先点图退出。
                _run_adb(
                    adb_exe=adb_exe,
                    serial=serial,
                    args=["shell", "input", "tap", "540", "1060"],
                    timeout_sec=service.adb_capture_timeout_sec,
                    capture_output=False,
                )
                time.sleep(0.35)
                check_xml = _dump_ui_xml(adb_exe=adb_exe, serial=serial, timeout_sec=service.adb_capture_timeout_sec)
                if not _xml_has_chat_input(check_xml):
                    _run_adb(
                        adb_exe=adb_exe,
                        serial=serial,
                        args=["shell", "input", "keyevent", "4"],
                        timeout_sec=service.adb_capture_timeout_sec,
                        capture_output=False,
                    )
                    time.sleep(0.25)
            except Exception:
                pass

    if proc.returncode != 0:
        return ""
    data = proc.stdout or b""
    if len(data) < 128:
        return ""
    if capture_mode == "chat_bounds_crop":
        if bounds is None:
            return ""
        cropped = _crop_png_bytes(data, bounds)
        if not cropped:
            return ""
        data = cropped
    saved = _persist_uploaded_media(
        service,
        content=data,
        mime_type="image/png",
        source_ref=f"adb:{serial}:{event.platform}:{event.store_id}:{event.message_id}",
    )
    if not isinstance(saved, dict):
        return ""
    return str(saved.get("media_path") or "").strip()


def _capture_screen_png_via_adb(
    *,
    service: ServiceState,
    device_serial: str,
) -> bytes:
    if not service.adb_capture_enabled:
        return b""
    adb_bin = str(service.adb_bin or "").strip()
    if not adb_bin:
        return b""
    adb_exe = Path(adb_bin)
    if not adb_exe.exists():
        return b""
    serial = str(device_serial or "").strip()
    if not serial:
        return b""
    proc = _run_adb(
        adb_exe=adb_exe,
        serial=serial,
        args=["exec-out", "screencap", "-p"],
        timeout_sec=service.adb_capture_timeout_sec,
        capture_output=True,
    )
    if proc.returncode != 0:
        return b""
    data = proc.stdout or b""
    if len(data) < 128:
        return b""
    return data


def _get_worker_ocr_cache(service: ServiceState, sha1: str) -> str | None:
    key = str(sha1 or "").strip().lower()
    if not key:
        return None
    now = time.time()
    with service._worker_ocr_cache_guard:
        entry = service._worker_ocr_cache.get(key)
        if entry is None:
            return None
        cached_at, text = entry
        if now - cached_at > service.worker_ocr_cache_ttl_sec:
            service._worker_ocr_cache.pop(key, None)
            return None
        return text


def _set_worker_ocr_cache(service: ServiceState, sha1: str, text: str) -> None:
    key = str(sha1 or "").strip().lower()
    if not key:
        return
    now = time.time()
    with service._worker_ocr_cache_guard:
        if len(service._worker_ocr_cache) >= service.worker_ocr_cache_max_entries:
            expired = [
                old_key
                for old_key, (cached_at, _) in service._worker_ocr_cache.items()
                if now - cached_at > service.worker_ocr_cache_ttl_sec
            ]
            for old_key in expired:
                service._worker_ocr_cache.pop(old_key, None)
            while len(service._worker_ocr_cache) >= service.worker_ocr_cache_max_entries:
                service._worker_ocr_cache.pop(next(iter(service._worker_ocr_cache)), None)
        service._worker_ocr_cache[key] = (now, str(text or "").strip())


def _parse_worker_ocr_reply(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith("```") and value.endswith("```"):
        lines = value.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(payload, dict):
        return str(payload.get("text") or "").strip()
    return value


def _extract_worker_ocr_text_with_openclaw(service: ServiceState, *, png_data: bytes) -> str:
    invoker = service.agent_invoker
    if invoker is None or not png_data or len(png_data) > invoker.attachment_max_bytes:
        return ""

    image_sha1 = hashlib.sha1(png_data).hexdigest()
    attachment = {
        "type": "image",
        "mimeType": "image/png",
        "fileName": f"worker-ocr-{image_sha1[:12]}.png",
        "content": base64.b64encode(png_data).decode("ascii"),
    }
    prompt = (
        "这是内部OCR任务，不是顾客消息。\n"
        "只识别本轮图片中的可见文字，不做客服回复，不调用工具。\n"
        "只输出JSON：{\"text\":\"识别出的文字\"}。\n"
        "识别不到时text为空字符串；多行按正常阅读顺序用换行连接；不要解释。"
    )
    try:
        reply = invoker.run_prompt(
            session_key=f"agent:{invoker.agent_id}:worker-ocr:{image_sha1}",
            prompt=prompt,
            attachments=[attachment],
        )
    except Exception as exc:
        print(f"[worker_ocr] openclaw failed: {type(exc).__name__}")
        return ""
    return _parse_worker_ocr_reply(reply.text)


def _extract_worker_ocr_text(service: ServiceState, *, png_data: bytes) -> str:
    if service.worker_ocr_provider == "openclaw":
        return _extract_worker_ocr_text_with_openclaw(service, png_data=png_data)

    image_ref = _png_bytes_to_data_url(png_data)
    if not image_ref:
        return ""

    if service.worker_ocr_provider == "hermes":
        client = service.worker_ocr_client
        if client is None or not client.enabled:
            return ""
        result = client.extract_text(image_url=image_ref)
    elif service.worker_ocr_provider in {"qwen", "qwen_vl"}:
        backend = service.tool_backend
        if backend is None:
            return ""
        result = backend.qwen_vision.extract_text(image_url=image_ref)
    else:
        return ""

    if not result.ok:
        return ""
    return str(result.data.get("text") or "").strip()


def _persist_worker_capture_batch_debug(
    *,
    service: ServiceState,
    device_serial: str,
    screen_png: bytes,
    item_records: list[dict[str, Any]],
) -> str:
    debug_root = service.media_root / "ocr_batch_debug"
    debug_root.mkdir(parents=True, exist_ok=True)
    batch_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}_{device_serial}"
    batch_dir = debug_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    fullscreen_path = batch_dir / "fullscreen.png"
    fullscreen_path.write_bytes(screen_png)

    items_dir = batch_dir / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, Any]] = []
    for idx, item in enumerate(item_records):
        item_id = str(item.get("item_id") or f"item_{idx}")
        safe_item_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", item_id).strip("._") or f"item_{idx}"
        cropped_png = item.get("cropped_png") or b""
        sha1 = str(item.get("sha1") or "")
        crop_name = f"{idx:02d}_{safe_item_id}"
        if sha1:
            crop_name += f"_{sha1[:8]}"
        crop_path = items_dir / f"{crop_name}.png"
        if cropped_png:
            crop_path.write_bytes(cropped_png)
            crop_path_str = str(crop_path)
        else:
            crop_path_str = ""
        manifest_items.append(
            {
                "item_id": item_id,
                "bounds": str(item.get("bounds") or ""),
                "ocr_text": str(item.get("ocr_text") or ""),
                "sha1": sha1,
                "bytes": int(item.get("bytes") or 0),
                "crop_path": crop_path_str,
            }
        )

    manifest = {
        "device_serial": device_serial,
        "saved_at_ms": now_ms(),
        "fullscreen_path": str(fullscreen_path),
        "items": manifest_items,
    }
    (batch_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(batch_dir)


def create_app() -> FastAPI:
    policy_file = os.getenv("CLAWBOT_POLICY_FILE", _bundled_path("config/policy.yaml"))
    db_path = os.getenv("CLAWBOT_DB_PATH", "data/clawbot.db")
    api_key = os.getenv("CLAWBOT_API_KEY", "").strip()
    decision_mode = _normalize_decision_mode(os.getenv("CLAWBOT_DECISION_MODE", "agent"))
    fallback_text = (
        os.getenv(
            "CLAWBOT_FALLBACK_TEXT",
            "稍等",
        ).strip()
    )
    image_no_text_reply = (
        os.getenv(
            "CLAWBOT_IMAGE_NO_TEXT_REPLY",
            "亲，图片我已收到，麻烦再发一下订单号，给您尽快处理。",
        ).strip()
    )
    tool_base_url = (os.getenv("CLAWBOT_TOOL_BASE_URL", "") or "").strip()
    tool_api_key = (os.getenv("CLAWBOT_TOOL_API_KEY", "") or "").strip()
    tool_timeout_sec = _safe_float(os.getenv("CLAWBOT_TOOL_TIMEOUT_SEC"), default=8.0)
    vision_provider = (os.getenv("CLAWBOT_VISION_PROVIDER", "disabled") or "").strip().lower()
    image_inspect_enabled = _as_bool(os.getenv("CLAWBOT_IMAGE_INSPECT_ENABLED", "0"))
    qwen_vl_api_key = (
        os.getenv("CLAWBOT_QWEN_VL_API_KEY", "")
        or os.getenv("DASHSCOPE_API_KEY", "")
        or ""
    ).strip()
    qwen_vl_model = (os.getenv("CLAWBOT_QWEN_VL_MODEL", "qwen3-vl-plus") or "").strip()
    qwen_vl_base_url = (
        os.getenv("CLAWBOT_QWEN_VL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1") or ""
    ).strip()
    vision_timeout_sec = _safe_float(
        os.getenv("CLAWBOT_VISION_TIMEOUT_SEC"),
        default=max(tool_timeout_sec, 15.0),
    )
    worker_ocr_provider = (os.getenv("CLAWBOT_WORKER_OCR_PROVIDER", "openclaw") or "").strip().lower()
    hermes_ocr_url = (os.getenv("CLAWBOT_HERMES_OCR_URL", "") or "").strip()
    hermes_ocr_route = (os.getenv("CLAWBOT_HERMES_OCR_ROUTE", "") or "").strip()
    hermes_ocr_model = (os.getenv("CLAWBOT_HERMES_OCR_MODEL", "") or "").strip()
    hermes_ocr_api_key = (os.getenv("CLAWBOT_HERMES_OCR_API_KEY", "") or "").strip()
    hermes_ocr_timeout_sec = _safe_float(
        os.getenv("CLAWBOT_HERMES_OCR_TIMEOUT_SEC"),
        default=60.0,
    )
    identity_base_url = (os.getenv("CLAWBOT_IDENTITY_BASE_URL", "") or "").strip()
    identity_org_id = (os.getenv("CLAWBOT_IDENTITY_ORG_ID", "") or "").strip()
    identity_room_name = (os.getenv("CLAWBOT_IDENTITY_ROOM_NAME", "") or "").strip()
    identity_api_key = (os.getenv("CLAWBOT_IDENTITY_API_KEY", "") or "").strip()
    kuaimai_erp_host = (os.getenv("CLAWBOT_KUAIMAI_ERP_HOST", "https://erp.superboss.cc") or "").strip()
    kuaimai_company_name = (os.getenv("CLAWBOT_KUAIMAI_COMPANY_NAME", "") or "").strip()
    kuaimai_account = (os.getenv("CLAWBOT_KUAIMAI_ACCOUNT", "") or "").strip()
    kuaimai_password = str(os.getenv("CLAWBOT_KUAIMAI_PASSWORD", "") or "").strip()
    kuaimai_company_id = (os.getenv("CLAWBOT_KUAIMAI_COMPANY_ID", "") or "").strip()
    kuaimai_snapshot_ttl_sec = _safe_float(os.getenv("CLAWBOT_KUAIMAI_SNAPSHOT_TTL_SEC"), default=300.0)
    kuaimai_storage_state = (
        os.getenv("CLAWBOT_KUAIMAI_STORAGE_STATE", "data/kuaimai_storage_state.json") or ""
    ).strip()
    kuaimai_login_headless = _as_bool(os.getenv("CLAWBOT_KUAIMAI_LOGIN_HEADLESS", "1"))
    kuaimai_chromium_path = (os.getenv("CLAWBOT_KUAIMAI_CHROMIUM_PATH", "") or "").strip()
    kuaimai_browser_channel = (os.getenv("CLAWBOT_KUAIMAI_BROWSER_CHANNEL", "chrome") or "").strip()
    history_days = _safe_int(os.getenv("CLAWBOT_HISTORY_DAYS"), default=2)
    history_days = max(1, min(history_days, 7))
    media_root = Path((os.getenv("CLAWBOT_MEDIA_ROOT", "data/media") or "data/media").strip()).expanduser()
    media_fetch_timeout_sec = _safe_float(os.getenv("CLAWBOT_MEDIA_FETCH_TIMEOUT_SEC"), default=12.0)
    media_max_bytes = _safe_int(os.getenv("CLAWBOT_MEDIA_MAX_BYTES"), default=12 * 1024 * 1024)
    media_max_bytes = max(256 * 1024, media_max_bytes)
    adb_capture_enabled = _as_bool(os.getenv("CLAWBOT_ADB_CAPTURE_ENABLED", "0"))
    adb_bin = (os.getenv("CLAWBOT_ADB_BIN", "adb") or "adb").strip()
    adb_capture_timeout_sec = _safe_float(os.getenv("CLAWBOT_ADB_CAPTURE_TIMEOUT_SEC"), default=5.0)
    wecom_enabled = _as_bool(os.getenv("CLAWBOT_WECOM_ENABLED", "0"))
    wecom_corp_id = (os.getenv("CLAWBOT_WECOM_CORP_ID", "") or "").strip()
    wecom_agent_id = (os.getenv("CLAWBOT_WECOM_AGENT_ID", "") or "").strip()
    wecom_app_secret = (os.getenv("CLAWBOT_WECOM_APP_SECRET", "") or "").strip()
    wecom_token = (os.getenv("CLAWBOT_WECOM_TOKEN", "") or "").strip()
    wecom_aes_key = (os.getenv("CLAWBOT_WECOM_AES_KEY", "") or "").strip()
    wecom_app_name = (os.getenv("CLAWBOT_WECOM_APP_NAME", "clawbot") or "").strip() or "clawbot"
    wecom_hub_url = (os.getenv("CLAWBOT_WECOM_HUB_URL", "") or "").strip().rstrip("/")
    wecom_hub_api_key = (os.getenv("CLAWBOT_WECOM_HUB_API_KEY", "") or "").strip()
    wecom_app_key = (
        os.getenv("CLAWBOT_WECOM_APP_KEY", "")
        or os.getenv("CLAWBOT_OPENCLAW_AGENT_ID", "")
        or "kefu_ops"
    ).strip()

    kuaimai_client = KuaimaiClient(
        erp_host=kuaimai_erp_host,
        company_name=kuaimai_company_name,
        account=kuaimai_account,
        password=kuaimai_password,
        company_id=kuaimai_company_id,
        timeout_sec=tool_timeout_sec,
        snapshot_ttl_sec=kuaimai_snapshot_ttl_sec,
        storage_state_path=kuaimai_storage_state,
        login_headless=kuaimai_login_headless,
        chromium_path=kuaimai_chromium_path,
        browser_channel=kuaimai_browser_channel,
    )
    qwen_vision_client = QwenVisionClient(
        api_key=qwen_vl_api_key if image_inspect_enabled and vision_provider == "qwen_vl" else "",
        model=qwen_vl_model,
        base_url=qwen_vl_base_url,
        timeout_sec=vision_timeout_sec,
    )
    worker_ocr_client: HermesOcrClient | None = None
    if worker_ocr_provider == "hermes":
        worker_ocr_client = HermesOcrClient(
            url=hermes_ocr_url,
            route=hermes_ocr_route,
            model=hermes_ocr_model,
            api_key=hermes_ocr_api_key,
            timeout_sec=hermes_ocr_timeout_sec,
        )

    tool_backend: ToolBackendService | None = ToolBackendService(
        identity_base_url=identity_base_url,
        identity_org_id=identity_org_id,
        identity_room_name=identity_room_name,
        identity_api_key=identity_api_key,
        kuaimai_client=kuaimai_client,
        qwen_vision_client=qwen_vision_client,
        timeout_sec=tool_timeout_sec,
        clawbot_db_path=str(Path(db_path).resolve()),
        admin_tools_enabled=_as_bool(os.getenv("CLAWBOT_ADMIN_TOOLS_ENABLED", "0")),
    )
    wecom_bridge: WeComBridge | None = None
    if wecom_enabled:
        wecom_bridge = WeComBridge(
            token=wecom_token,
            encoding_aes_key=wecom_aes_key,
            corp_id=wecom_corp_id,
            app_secret=wecom_app_secret,
            agent_id=wecom_agent_id,
            timeout_sec=tool_timeout_sec,
        )

    agent_invoker: OpenClawAgentInvoker | None = None
    if decision_mode == "agent":
        openclaw_bin = os.getenv("CLAWBOT_OPENCLAW_BIN", "openclaw")
        openclaw_agent_id = os.getenv("CLAWBOT_OPENCLAW_AGENT_ID", "kefu_ops").strip() or "kefu_ops"
        openclaw_timeout_sec = _safe_float(os.getenv("CLAWBOT_OPENCLAW_TIMEOUT_SEC"), default=120.0)
        openclaw_local = _as_bool(os.getenv("CLAWBOT_OPENCLAW_LOCAL", "1"))
        openclaw_attachment_max_bytes = _safe_int(
            os.getenv("CLAWBOT_OPENCLAW_ATTACHMENT_MAX_BYTES"),
            default=DEFAULT_ATTACHMENT_MAX_BYTES,
        )
        agent_invoker = OpenClawAgentInvoker(
            openclaw_bin=openclaw_bin,
            agent_id=openclaw_agent_id,
            timeout_sec=openclaw_timeout_sec,
            use_local=openclaw_local,
            attachment_max_bytes=openclaw_attachment_max_bytes,
        )

    app = FastAPI(title="kefu-clawbot", version=__version__)
    app.state.service = ServiceState(
        policy=PolicyEngine.from_file(policy_file),
        store=SQLiteStore(Path(db_path)),
        api_key=api_key,
        decision_mode=decision_mode,
        fallback_text=fallback_text,
        image_no_text_reply=image_no_text_reply,
        agent_invoker=agent_invoker,
        tool_backend=tool_backend,
        worker_ocr_provider=worker_ocr_provider,
        worker_ocr_client=worker_ocr_client,
        tool_api_key=tool_api_key,
        tool_base_url=tool_base_url,
        identity_base_url=identity_base_url,
        kuaimai_erp_host=kuaimai_erp_host,
        kuaimai_browser_channel=kuaimai_browser_channel,
        history_days=history_days,
        wecom_bridge=wecom_bridge,
        wecom_app_name=wecom_app_name,
        wecom_hub_url=wecom_hub_url,
        wecom_hub_api_key=wecom_hub_api_key,
        wecom_app_key=wecom_app_key,
        media_root=media_root,
        media_fetch_timeout_sec=media_fetch_timeout_sec,
        media_max_bytes=media_max_bytes,
        adb_capture_enabled=adb_capture_enabled,
        adb_bin=adb_bin,
        adb_capture_timeout_sec=adb_capture_timeout_sec,
    )

    @app.get("/")
    def root() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "openclaw-agent-bridge",
            "version": __version__,
        }

    @app.get("/health")
    def health() -> dict[str, Any]:
        service: ServiceState = app.state.service
        local_backend = service.tool_backend
        openclaw_bin = service.agent_invoker.openclaw_bin if service.agent_invoker else ""
        openclaw_cli_available = bool(
            openclaw_bin and (shutil.which(openclaw_bin) or Path(openclaw_bin).expanduser().is_file())
        )
        if service.worker_ocr_provider == "openclaw":
            worker_ocr_enabled = bool(service.agent_invoker)
        elif service.worker_ocr_provider == "hermes":
            worker_ocr_enabled = bool(service.worker_ocr_client and service.worker_ocr_client.enabled)
        elif service.worker_ocr_provider in {"qwen", "qwen_vl"}:
            worker_ocr_enabled = bool(local_backend and local_backend.qwen_vision.enabled)
        else:
            worker_ocr_enabled = False
        return {
            "ok": True,
            "service": "openclaw-agent-bridge",
            "version": __version__,
            "uptime_ms": now_ms() - service.started_at_ms,
            "db_path": str(service.store.db_path),
            "decision_mode": service.decision_mode,
            "openclaw_agent_id": service.agent_invoker.agent_id if service.agent_invoker else "",
            "openclaw_cli_available": openclaw_cli_available,
            "tool_base_url": service.tool_base_url,
            "identity_base_url": service.identity_base_url,
            "kuaimai_erp_host": service.kuaimai_erp_host,
            "kuaimai_browser_channel": service.kuaimai_browser_channel,
            "history_days": service.history_days,
            "vision_enabled": bool(local_backend and local_backend.qwen_vision.enabled),
            "vision_model": local_backend.qwen_vision.model if local_backend else "",
            "worker_ocr_provider": service.worker_ocr_provider,
            "worker_ocr_enabled": worker_ocr_enabled,
            "wecom_enabled": bool(service.wecom_bridge is not None or service.wecom_hub_url),
            "wecom_mode": "hub_proxy" if service.wecom_hub_url else ("local" if service.wecom_bridge else "disabled"),
            "wecom_hub_url": service.wecom_hub_url,
            "wecom_hub_api_key_configured": bool(service.wecom_hub_api_key),
            "wecom_app_key": service.wecom_app_key,
            "media_root": str(service.media_root),
            "adb_capture_enabled": service.adb_capture_enabled,
            "adb_bin": service.adb_bin,
        }

    @app.post("/v1/tools/{tool_name}")
    def tool_call(
        tool_name: str,
        payload: dict[str, Any],
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        if service.tool_api_key and (x_api_key or "") != service.tool_api_key:
            raise HTTPException(status_code=403, detail="invalid_tool_api_key")
        try:
            return _invoke_tool(
                service=service,
                tool_name=tool_name,
                payload=payload,
                timeout_sec=8.0,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            return {
                "ok": False,
                "message": f"tool_backend_exception:{type(exc).__name__}",
                "data": {},
            }

    @app.post("/v1/intercept/current")
    def intercept_current(
        payload: dict[str, Any] | None = None,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        if service.tool_api_key and (x_api_key or "") != service.tool_api_key:
            raise HTTPException(status_code=403, detail="invalid_tool_api_key")
        backend = service.tool_backend
        if backend is None:
            return {
                "ok": False,
                "message": "tool_backend_not_configured",
                "data": {},
            }
        try:
            return build_intercept_snapshot(
                backend=backend,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            return {
                "ok": False,
                "message": f"intercept_snapshot_exception:{type(exc).__name__}",
                "data": {},
            }

    @app.post("/v1/worker/capture_ocr_batch")
    def worker_capture_ocr_batch(
        request: WorkerCaptureOcrBatchRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)

        items = list(request.items or [])
        if not items:
            return {"ok": True, "items": []}

        screen_png = _capture_screen_png_via_adb(
            service=service,
            device_serial=request.device_serial,
        )
        if not screen_png:
            return {
                "ok": False,
                "message": "adb_capture_failed",
                "items": [{"item_id": item.item_id, "ocr_text": "", "sha1": ""} for item in items],
            }

        results: list[dict[str, str]] = []
        debug_items: list[dict[str, Any]] = []
        for item in items:
            bounds_sig = str(item.bounds or "").strip()
            cropped_png = b""
            if bounds_sig.lower() == "fullscreen":
                cropped_png = screen_png
            else:
                bounds = _parse_bounds(bounds_sig)
                if bounds is not None:
                    cropped_png = _crop_png_bytes(screen_png, bounds)
            sha1 = hashlib.sha1(cropped_png).hexdigest() if cropped_png else ""
            cache_hit = False
            if cropped_png and sha1:
                cached_text = _get_worker_ocr_cache(service, sha1)
                if cached_text is not None:
                    ocr_text = cached_text
                    cache_hit = True
                else:
                    ocr_text = _extract_worker_ocr_text(service, png_data=cropped_png)
                    _set_worker_ocr_cache(service, sha1, ocr_text)
            else:
                ocr_text = ""
            persisted = (
                _persist_uploaded_media(
                    service,
                    content=cropped_png,
                    mime_type="image/png",
                    source_ref=f"worker_capture_batch:{request.device_serial}:{sha1}",
                )
                if cropped_png
                else None
            )
            persisted_sha1 = str((persisted or {}).get("sha1") or "").strip()
            if not sha1:
                sha1 = persisted_sha1
            preview = re.sub(r"\s+", " ", ocr_text).strip()[:80]
            print(
                "[worker_capture_ocr_batch] "
                f"device={request.device_serial} item_id={item.item_id} "
                f"bounds='{bounds_sig}' bytes={len(cropped_png)} sha1={sha1 or '-'} "
                f"cache={'hit' if cache_hit else 'miss'} ocr='{preview}'"
            )
            debug_items.append(
                {
                    "item_id": item.item_id,
                    "bounds": bounds_sig,
                    "ocr_text": ocr_text,
                    "sha1": sha1,
                    "bytes": len(cropped_png),
                    "cropped_png": cropped_png,
                }
            )
            results.append(
                {
                    "item_id": item.item_id,
                    "ocr_text": ocr_text,
                    "sha1": sha1,
                }
            )

        debug_dir = _persist_worker_capture_batch_debug(
            service=service,
            device_serial=request.device_serial,
            screen_png=screen_png,
            item_records=debug_items,
        )
        print(
            "[worker_capture_ocr_batch] "
            f"device={request.device_serial} debug_dir='{debug_dir}' items={len(debug_items)}"
        )

        return {
            "ok": True,
            "items": results,
        }

    @app.post("/v1/decide")
    def decide(
        request: DecisionRequest,
        x_trace_id: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)
        return _run_decision(service, request.event, trace_id=x_trace_id)

    @app.post("/v1/agent/prompt")
    def agent_prompt(
        request: AgentPromptRequest,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)
        invoker = service.agent_invoker
        if invoker is None:
            raise HTTPException(status_code=503, detail="agent_invoker_not_configured")

        session_key_raw = str(request.session_key or "").strip()
        if not session_key_raw:
            session_key = f"agent:{invoker.agent_id}:prompt:{uuid.uuid4().hex}"
        elif session_key_raw.startswith("agent:"):
            session_key = session_key_raw
        else:
            session_key = f"agent:{invoker.agent_id}:{session_key_raw}"
        attachments_raw = request.attachments if isinstance(request.attachments, list) else []
        attachments: list[dict[str, str]] = []
        for item in attachments_raw:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").strip().lower()
            mime_type = str(item.get("mimeType") or item.get("mime_type") or "").strip()
            file_name = str(item.get("fileName") or item.get("file_name") or "").strip()
            content = str(item.get("content") or "").strip()
            if kind != "image" or (not mime_type) or (not file_name) or (not content):
                continue
            attachments.append(
                {
                    "type": "image",
                    "mimeType": mime_type,
                    "fileName": file_name,
                    "content": content,
                }
            )

        try:
            reply = invoker.run_prompt(
                session_key=session_key,
                prompt=request.prompt,
                attachments=attachments or None,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"openclaw_call_failed:{type(exc).__name__}:{str(exc)[:300]}",
            ) from exc

        reply_text = str(reply.text or "")
        reply_json: dict[str, Any] | list[Any] | None = None
        try:
            parsed = json.loads(reply_text)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            reply_json = parsed

        return {
            "ok": True,
            "agent_id": invoker.agent_id,
            "session_key": reply.session_key,
            "reply_text": reply_text,
            "reply_json": reply_json,
            "is_json": isinstance(reply_json, (dict, list)),
        }

    @app.get("/v1/wecom/callback")
    def wecom_verify(
        request: Request,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> PlainTextResponse:
        service: ServiceState = app.state.service
        if service.wecom_hub_url:
            return _proxy_wecom_verify(service, request)
        bridge = service.wecom_bridge
        if bridge is None:
            raise HTTPException(status_code=404, detail="wecom_not_enabled")
        try:
            plain = bridge.verify_url(
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                echostr=echostr,
            )
            return PlainTextResponse(plain)
        except Exception as exc:
            raise HTTPException(status_code=403, detail=f"wecom_verify_failed:{type(exc).__name__}") from exc

    @app.post("/v1/wecom/callback")
    async def wecom_callback(
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> PlainTextResponse:
        service: ServiceState = app.state.service
        if service.wecom_hub_url:
            body = await request.body()
            return _proxy_wecom_callback(service, request, body)
        bridge = service.wecom_bridge
        if bridge is None:
            raise HTTPException(status_code=404, detail="wecom_not_enabled")

        msg_signature = str(request.query_params.get("msg_signature") or "")
        timestamp = str(request.query_params.get("timestamp") or "")
        nonce = str(request.query_params.get("nonce") or "")
        if not msg_signature or not timestamp or not nonce:
            raise HTTPException(status_code=400, detail="wecom_callback_query_missing")

        body = await request.body()
        body_xml = body.decode("utf-8", errors="ignore")
        try:
            msg = bridge.parse_callback_message(
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                body_xml=body_xml,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"wecom_callback_decode_failed:{type(exc).__name__}") from exc

        if (not bridge.should_process(msg)) or (not bridge.mark_once(msg)):
            return PlainTextResponse("success")

        background_tasks.add_task(_process_wecom_message, service, msg)
        return PlainTextResponse("success")

    @app.get("/v1/ops/summary")
    def ops_summary(
        minutes: int = 60,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)
        return service.store.summary(minutes=minutes)

    @app.get("/v1/ops/recent")
    def ops_recent(
        limit: int = 20,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)
        return {"items": service.store.recent_sessions(limit=limit, include_wecom=False)}

    @app.get("/v1/ops/trace/{trace_id}")
    def ops_trace(
        trace_id: str,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)
        payload = service.store.trace(trace_id=trace_id)
        if payload is None:
            raise HTTPException(status_code=404, detail="trace_not_found")
        return payload

    @app.get("/v1/wecom/usage")
    def wecom_usage(
        member_id: str = "",
        minutes: int = 60,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)
        return service.store.wecom_usage(member_id=member_id, minutes=minutes)

    @app.post("/v1/worker/ack")
    def worker_ack(
        payload: dict[str, Any],
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: ServiceState = app.state.service
        _check_api_key(service, x_api_key)
        event = payload.get("event")
        if not isinstance(event, dict):
            event = {}

        tenant_id = str(event.get("tenant_id") or payload.get("tenant_id") or "default").strip() or "default"
        platform = str(event.get("platform") or payload.get("platform") or "").strip()
        store_id = str(event.get("store_id") or payload.get("store_id") or "").strip()
        session_key = str(event.get("session_key") or payload.get("session_key") or "").strip()
        message_id = str(event.get("message_id") or payload.get("message_id") or "").strip()
        trace_id = str(payload.get("trace_id") or payload.get("decision_trace_id") or "").strip()
        status = str(payload.get("status") or "sent").strip() or "sent"
        reason = str(payload.get("reason") or "").strip()
        sent_text = str(payload.get("sent_text") or "").strip()
        sent_image_urls = _coerce_image_urls(payload.get("sent_image_urls"))
        sent_at_ms = _safe_int(payload.get("sent_at_ms"), default=now_ms())

        if (not platform) or (not store_id) or (not session_key) or (not message_id):
            return {"ok": False, "message": "ack_missing_identity", "data": {}}

        service.store.record_delivery(
            tenant_id=tenant_id,
            platform=platform,
            store_id=store_id,
            session_key=session_key,
            message_id=message_id,
            trace_id=trace_id,
            status=status,
            sent_text=sent_text,
            sent_image_urls=sent_image_urls,
            reason=reason,
            sent_at_ms=sent_at_ms,
        )
        return {"ok": True, "message": "ack_recorded", "data": {}}

    return app


def _run_decision(
    service: ServiceState,
    event: IncomingEvent,
    *,
    trace_id: str | None,
) -> dict[str, Any]:
    event_with_media = _materialize_event_media(service, event)
    event_with_caps = _event_with_channel_capabilities(event=event_with_media)
    session_lock = service.get_session_lock(event_with_caps.session_key())
    with session_lock:
        if not _bypass_decision_cache(event_with_caps):
            cached = service.store.lookup_cached_decision(
                tenant_id=event_with_caps.tenant_id,
                platform=event_with_caps.platform,
                store_id=event_with_caps.store_id,
                message_id=event_with_caps.message_id,
            )
            if cached is not None:
                extra = dict(cached.get("extra") or {})
                extra.update(
                    {
                        "category": cached["category"],
                        "confidence": cached["confidence"],
                        "amount": cached["amount"],
                        "need_more": cached["need_more"],
                        "role": event_with_caps.role_key(),
                        "channel_capabilities": _extract_channel_capabilities(event_with_caps),
                        "trace_id": cached["trace_id"],
                        "session_key": event_with_caps.session_key(),
                        "cache_hit": True,
                    }
                )
                return DecisionResponse(
                    action=str(cached["action"]),
                    reply_text=str(cached["reply_text"]),
                    reason=f"{cached['reason']}:cache_hit",
                    reply_image_urls=_extract_image_urls_from_extra(extra),
                    extra=extra,
                ).model_dump()

        trace_id_norm = _normalize_trace_id(trace_id)
        category_result = classify_event(event_with_caps)
        decision = _decide(service, event_with_caps, category_result, trace_id_norm)
        decision.extra.update(
            {
                "category": decision.category,
                "confidence": decision.confidence,
                "amount": decision.amount,
                "need_more": decision.need_more,
                "role": event_with_caps.role_key(),
                "channel_capabilities": _extract_channel_capabilities(event_with_caps),
                "trace_id": trace_id_norm,
                "session_key": event_with_caps.session_key(),
            }
        )
        try:
            service.store.insert_event_and_decision(trace_id_norm, event_with_caps, decision)
        except Exception as exc:  # pragma: no cover
            decision.reason = f"{decision.reason}|store_error:{type(exc).__name__}"
        payload = DecisionResponse(
            action=decision.action,
            reply_text=decision.reply_text,
            reason=decision.reason,
            reply_image_urls=_extract_image_urls_from_extra(decision.extra),
            extra=decision.extra,
        )
        return payload.model_dump()


def _build_wecom_event(service: ServiceState, msg: WeComIncomingMessage) -> IncomingEvent:
    message_type = "image" if msg.msg_type == "image" else "text"
    text = msg.content if msg.msg_type == "text" else ""
    media_url = msg.pic_url if msg.msg_type == "image" else ""
    msg_id = msg.msg_id.strip()
    if not msg_id:
        stable = "|".join(
            [
                msg.from_user,
                msg.to_user,
                msg.msg_type,
                msg.create_time,
                text,
                media_url,
                msg.event,
                msg.event_key,
            ]
        )
        msg_id = "wecom_" + hashlib.sha1(stable.encode("utf-8")).hexdigest()[:24]
    return IncomingEvent(
        tenant_id="default",
        platform="wecom",
        store_id=f"wecom_app_{msg.agent_id or 'default'}",
        store_name=service.wecom_app_name,
        role="admin",
        customer_id=msg.from_user,
        platform_nickname=msg.from_user,
        message_id=msg_id,
        message_type=message_type,
        text=text,
        media_url=media_url,
        raw={
            "wecom_msg_type": msg.msg_type,
            "create_time": msg.create_time,
            "event": msg.event,
            "event_key": msg.event_key,
            "channel_capabilities": {
                "channel": "wecom",
                "platform": "wecom",
                "send_text": True,
                "send_image": True,
                "send_image_input": "image_url",
                "max_image_bytes": 2 * 1024 * 1024,
            },
            "raw": msg.raw,
        },
    )


def _process_wecom_message(service: ServiceState, msg: WeComIncomingMessage) -> None:
    bridge = service.wecom_bridge
    if bridge is None:
        return
    if msg.msg_type == "event":
        return
    event = _build_wecom_event(service, msg)
    try:
        payload = _run_decision(service, event, trace_id=None)
    except Exception as exc:
        reply = _safe_wecom_fallback_text(service.fallback_text)
        _save_wecom_fallback_trace(
            service=service,
            event=event,
            reply_text=reply,
            reason=f"wecom_decision_fallback:{type(exc).__name__}",
            extra={"error": str(exc)[:300]},
        )
        sent_ok, sent_payload = _send_wecom_text_chunks(
            service,
            to_user=msg.from_user,
            content=reply,
        )
        if sent_ok:
            service.store.record_delivery(
                tenant_id=event.tenant_id,
                platform=event.platform,
                store_id=event.store_id,
                session_key=event.session_key(),
                message_id=event.message_id,
                trace_id="",
                status="sent",
                sent_text=sent_payload,
                sent_image_urls=[],
                reason=f"wecom_decision_fallback:{type(exc).__name__}",
            )
        return

    action = str(payload.get("action") or "").strip().lower()
    reply = str(payload.get("reply_text") or "").strip()
    image_urls = _extract_reply_image_urls(payload)
    reason = str(payload.get("reason") or "").strip()
    extra = payload.get("extra")
    extra_dict = extra if isinstance(extra, dict) else {}
    trace_id = str(extra_dict.get("trace_id") or "").strip()
    if action == "send":
        if (not reply) and (not image_urls):
            reply = _safe_wecom_fallback_text(service.fallback_text)
        sent_text = ""
        sent_images: list[str] = []
        if reply:
            sent_ok, sent_payload = _send_wecom_text_chunks(
                service,
                to_user=msg.from_user,
                content=reply,
            )
            if sent_ok:
                sent_text = sent_payload
        for image_url in image_urls:
            if _send_wecom_image_url(service, to_user=msg.from_user, image_url=image_url):
                sent_images.append(image_url)
        if sent_text or sent_images:
            service.store.record_delivery(
                tenant_id=event.tenant_id,
                platform=event.platform,
                store_id=event.store_id,
                session_key=event.session_key(),
                message_id=event.message_id,
                trace_id=trace_id,
                status="sent",
                sent_text=sent_text,
                sent_image_urls=sent_images,
                reason=reason,
            )


def _safe_wecom_fallback_text(raw: str) -> str:
    text = str(raw or "").strip()
    if (not text) or ("客服休息" in text):
        return "稍等"
    return text


def _save_wecom_fallback_trace(
    *,
    service: ServiceState,
    event: IncomingEvent,
    reply_text: str,
    reason: str,
    extra: dict[str, Any],
) -> None:
    trace_id = _normalize_trace_id(None)
    decision = DecisionInternal(
        action="send",
        reply_text=str(reply_text or "").strip(),
        reason=reason,
        category="other",
        confidence=0.5,
        amount=0.0,
        need_more=False,
        manual=False,
        extra={"strategy": "wecom_decide_fallback", **extra},
    )
    try:
        service.store.insert_event_and_decision(trace_id, event, decision)
    except Exception:
        return

def _send_wecom_text(service: ServiceState, *, to_user: str, content: str) -> bool:
    bridge = service.wecom_bridge
    if bridge is None:
        return False
    try:
        send_ret = bridge.send_text(to_user=to_user, content=content)
        if not bool(send_ret.get("ok")):
            print(f"[wecom] send_failed to={to_user} detail={send_ret}", flush=True)
            return False
        service.store.record_wecom_send(to_user)
        return True
    except Exception as exc:
        print(f"[wecom] send_exception to={to_user} err={type(exc).__name__}:{exc}", flush=True)
        return False


def _send_wecom_text_chunks(
    service: ServiceState,
    *,
    to_user: str,
    content: str,
    max_bytes: int = 1800,
    max_chunks: int = 8,
) -> tuple[bool, str]:
    chunks = _split_text_by_utf8_bytes(
        content=str(content or "").strip(),
        max_bytes=max(200, int(max_bytes)),
        max_chunks=max(1, int(max_chunks)),
    )
    if not chunks:
        return False, ""

    sent: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        ok = _send_wecom_text(service, to_user=to_user, content=chunk)
        if not ok:
            print(
                f"[wecom] send_chunk_failed to={to_user} idx={idx}/{len(chunks)}",
                flush=True,
            )
            break
        sent.append(chunk)
    if not sent:
        return False, ""
    return True, "\n".join(sent).strip()


def _send_wecom_image_url(service: ServiceState, *, to_user: str, image_url: str) -> bool:
    bridge = service.wecom_bridge
    if bridge is None:
        return False
    try:
        send_ret = bridge.send_image_url(to_user=to_user, image_url=image_url)
        if not bool(send_ret.get("ok")):
            print(f"[wecom] send_image_failed to={to_user} url={image_url} detail={send_ret}", flush=True)
            return False
        service.store.record_wecom_send(to_user)
        return True
    except Exception as exc:
        print(f"[wecom] send_image_exception to={to_user} url={image_url} err={type(exc).__name__}:{exc}", flush=True)
        return False


def _extract_reply_image_urls(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def _append(raw: Any) -> None:
        val = str(raw or "").strip()
        if not val:
            return
        if not re.match(r"^https?://", val, flags=re.IGNORECASE):
            return
        if val in out:
            return
        out.append(val)

    raw_top = payload.get("reply_image_urls")
    if isinstance(raw_top, list):
        for item in raw_top:
            _append(item)
    elif isinstance(raw_top, str):
        _append(raw_top)

    extra = payload.get("extra")
    if isinstance(extra, dict):
        raw = extra.get("reply_image_urls")
        if isinstance(raw, list):
            for item in raw:
                _append(item)
        elif isinstance(raw, str):
            _append(raw)
        _append(extra.get("reply_image_url"))
        _append(extra.get("image_url"))
    return out


def _extract_image_urls_from_extra(extra: dict[str, Any]) -> list[str]:
    if not isinstance(extra, dict):
        return []
    out: list[str] = []
    for source in (extra.get("reply_image_urls"), extra.get("reply_image_url"), extra.get("image_url")):
        if isinstance(source, list):
            for item in source:
                val = str(item or "").strip()
                if val and re.match(r"^https?://", val, flags=re.IGNORECASE) and val not in out:
                    out.append(val)
        elif isinstance(source, str):
            val = source.strip()
            if val and re.match(r"^https?://", val, flags=re.IGNORECASE) and val not in out:
                out.append(val)
    return out


def _coerce_image_urls(raw: Any) -> list[str]:
    out: list[str] = []
    if isinstance(raw, list):
        sources = raw
    elif isinstance(raw, str):
        sources = [raw]
    else:
        sources = []
    for item in sources:
        val = str(item or "").strip()
        if not val:
            continue
        if not re.match(r"^https?://", val, flags=re.IGNORECASE):
            continue
        if val in out:
            continue
        out.append(val)
    return out



def _decide(
    service: ServiceState,
    event: IncomingEvent,
    category_result: CategoryResult,
    trace_id: str,
) -> DecisionInternal:
    if service.decision_mode == "policy":
        return service.policy.decide(event, category_result, trace_id)

    text = (event.text or "").strip()
    is_image_msg = str(event.message_type or "").strip().lower() == "image"

    if (not text) and (not is_image_msg):
        return DecisionInternal(
            action="skip",
            reply_text="",
            reason="empty_text",
            category=category_result.category,
            confidence=category_result.confidence,
            amount=0.0,
            need_more=False,
            manual=False,
            extra={"strategy": "agent", "trace_id": trace_id},
        )

    if service.agent_invoker is None:
        raise RuntimeError("agent_invoker_not_configured")

    return _decide_via_openclaw_single(
        service=service,
        event=event,
        category_result=category_result,
        trace_id=trace_id,
    )


def _decide_via_openclaw_single(
    *,
    service: ServiceState,
    event: IncomingEvent,
    category_result: CategoryResult,
    trace_id: str,
) -> DecisionInternal:
    invoker = service.agent_invoker
    if invoker is None:
        raise RuntimeError("agent_invoker_not_configured")

    openclaw_session_key = invoker.build_session_key(event)
    try:
        history = service.store.recent_dialogue(event.session_key(), limit=8)
    except Exception:
        history = []
    prompt, attachments = _build_openclaw_gateway_input(
        event=event,
        invoker=invoker,
        session_key=openclaw_session_key,
        history=history,
    )

    image_summary_retry_used = False
    try:
        raw_reply = invoker.run_prompt(
            session_key=openclaw_session_key,
            prompt=prompt,
            attachments=attachments,
        ).text.strip()
    except Exception as exc:
        if attachments and _is_unsupported_attachment_error(exc):
            try:
                raw_reply = _run_openclaw_image_summary_retry(
                    invoker=invoker,
                    event=event,
                    session_key=openclaw_session_key,
                    attachments=attachments,
                    history=history,
                ).strip()
                image_summary_retry_used = True
            except Exception as retry_exc:
                raise RuntimeError(
                    "openclaw_call_failed:"
                    f"{type(exc).__name__}:{str(exc)[:220]}; "
                    "image_summary_retry_failed:"
                    f"{type(retry_exc).__name__}:{str(retry_exc)[:220]}"
                ) from retry_exc
        else:
            raise RuntimeError(
                f"openclaw_call_failed:{type(exc).__name__}:{str(exc)[:300]}"
            ) from exc

    reply_text, reply_image_urls = _extract_image_urls_from_text(raw_reply)
    if not _channel_can_send_image(event):
        reply_image_urls = []
    reply_text = str(reply_text or "").strip()

    if reply_text and looks_like_json_reply(reply_text):
        raise RuntimeError(f"openclaw_invalid_reply_json:{reply_text[:300]}")

    if (not reply_text) and (not reply_image_urls):
        raise RuntimeError("openclaw_empty_reply")

    return DecisionInternal(
        action="send" if (reply_text or reply_image_urls) else "skip",
        reply_text=reply_text,
        reason="agent_single_reply",
        category=category_result.category,
        confidence=category_result.confidence,
        amount=0.0,
        need_more=False,
        manual=False,
        extra={
            "strategy": "agent_single_image_summary_retry"
            if image_summary_retry_used
            else "agent_single",
            "trace_id": trace_id,
            "openclaw_session_key": openclaw_session_key,
            "reply_image_urls": reply_image_urls,
            "image_summary_retry_used": image_summary_retry_used,
        },
    )


def _build_openclaw_gateway_input(
    *,
    event: IncomingEvent,
    invoker: OpenClawAgentInvoker,
    session_key: str,
    history: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    del session_key
    attachments = build_gateway_attachments_for_event(
        event,
        max_bytes=invoker.attachment_max_bytes,
    )
    image_input_mode = "attachment" if attachments else "missing"
    if str(event.message_type or "").strip().lower() != "image":
        image_input_mode = "none"
    prompt = _build_openclaw_gateway_prompt(
        event=event,
        image_input_mode=image_input_mode,
        history=history or [],
    )
    return (prompt, attachments)


def _run_openclaw_image_summary_retry(
    *,
    invoker: OpenClawAgentInvoker,
    event: IncomingEvent,
    session_key: str,
    attachments: list[dict[str, str]],
    history: list[dict[str, Any]],
) -> str:
    summary_session_key = f"{session_key}:image-summary:{event.message_id or uuid.uuid4().hex}"
    image_summary = invoker.run_prompt(
        session_key=summary_session_key,
        prompt=_build_openclaw_image_summary_prompt(event),
        attachments=attachments,
    ).text.strip()
    if not image_summary:
        raise RuntimeError("empty_image_summary")

    prompt = _build_openclaw_gateway_prompt(
        event=event,
        image_input_mode="summary",
        history=history,
        image_summary=image_summary,
    )
    return invoker.run_prompt(
        session_key=session_key,
        prompt=prompt,
        attachments=None,
    ).text


def _build_openclaw_image_summary_prompt(event: IncomingEvent) -> str:
    text = str(event.text or "").strip()
    return (
        "请只描述本轮图片内容，用于客服后续判断。\n"
        "不要输出客服回复话术，不要承诺处理结果。\n"
        "如果图片看不清、无法确认、文字模糊，必须明确说明不确定。\n\n"
        f"平台: {event.platform}\n"
        f"店铺名: {event.store_name}\n"
        f"顾客昵称: {event.platform_nickname}\n"
        f"顾客随图文字: {text or '（无）'}\n"
    )


def _is_unsupported_attachment_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return (
        "unsupportedattachmenterror" in text
        or "does not accept image input" in text
        or "does not accept image inputs" in text
    )


def _build_openclaw_gateway_prompt(
    *,
    event: IncomingEvent,
    image_input_mode: str = "none",
    history: list[dict[str, Any]] | None = None,
    image_summary: str = "",
) -> str:
    caps = _extract_channel_capabilities(event)
    can_send_image = bool(caps.get("send_image"))
    role_label = "管理员" if event.role_key() == "admin" else "顾客"
    current_msg = _render_event(event, image_input_mode=image_input_mode)
    history_block = _render_openclaw_history(history or [], event)
    image_summary_block = ""
    if image_summary:
        image_summary_block = (
            "图片内容摘要（由 OpenClaw 隔离视觉会话生成，仅作本轮图片证据；"
            "若摘要不确定，不要编造）：\n"
            f"{_clip_openclaw_history_text(image_summary, limit=800)}\n"
        )
    media_status = "（无）"
    if str(event.message_type or "").strip().lower() == "image":
        if image_input_mode == "attachment":
            media_status = "（已通过原始图片附件传入）"
        elif image_input_mode == "missing":
            media_status = "（未成功传入图片附件）"
        elif image_input_mode == "summary":
            media_status = "（原始图片已转为文字摘要传入）"
    prompt = (
        "请按你已加载的系统规则与技能处理这条消息。\n"
        "只输出最终可发送内容（纯文本）。\n"
        "如需发送图片，使用单独一行：[[IMAGE_URL:https://...]]（可多行）。\n\n"
        f"平台: {event.platform}\n"
        f"店铺ID: {event.store_id}\n"
        f"店铺名: {event.store_name}\n"
        f"顾客昵称: {event.platform_nickname}\n"
        f"消息角色: {role_label}\n"
        f"渠道能力: send_text=true, send_image={str(can_send_image).lower()}\n"
        f"消息类型: {event.message_type}\n"
        f"图片输入: {media_status}\n"
        f"最近对话:\n{history_block}\n"
        f"{image_summary_block}"
        f"当前消息:\n{current_msg}\n"
    )
    return prompt


def _render_openclaw_history(history: list[dict[str, Any]], event: IncomingEvent) -> str:
    if not history:
        return "（无）"
    is_admin = event.role_key() == "admin"
    user_tag = "管理员" if is_admin else "顾客"
    bot_tag = "助手" if is_admin else "客服"
    lines: list[str] = []
    for item in history:
        customer = _clip_openclaw_history_text(str(item.get("customer") or "").strip())
        agent = _clip_openclaw_history_text(str(item.get("agent") or "").strip())
        if customer:
            lines.append(f"{user_tag}: {customer}")
        if agent:
            lines.append(f"{bot_tag}: {agent}")
    return "\n".join(lines) if lines else "（无）"


def _clip_openclaw_history_text(text: str, *, limit: int = 240) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _bypass_decision_cache(event: IncomingEvent) -> bool:
    msg_type = str(event.message_type or "").strip().lower()
    text = str(event.text or "").strip()
    return msg_type == "image" and not text


def _extract_image_urls_from_text(text: str) -> tuple[str, list[str]]:
    raw = str(text or "")
    urls: list[str] = []

    def _append(url_raw: str) -> None:
        val = str(url_raw or "").strip()
        if not re.match(r"^https?://", val, flags=re.IGNORECASE):
            return
        if val in urls:
            return
        urls.append(val)

    def _marker_repl(match: re.Match[str]) -> str:
        _append(match.group(1))
        return ""

    text_wo_markers = IMAGE_URL_MARKER_RE.sub(_marker_repl, raw)
    for m in MARKDOWN_IMAGE_RE.finditer(text_wo_markers):
        _append(m.group(1))
    text_wo_images = MARKDOWN_IMAGE_RE.sub("", text_wo_markers)
    cleaned = re.sub(r"\n{3,}", "\n\n", text_wo_images).strip()
    return cleaned, urls


def _channel_can_send_image(event: IncomingEvent) -> bool:
    caps = _extract_channel_capabilities(event)
    return bool(caps.get("send_image"))


def _normalize_trace_id(trace_id: str | None) -> str:
    value = (trace_id or "").strip()
    if value:
        return value[:64]
    return uuid.uuid4().hex


def _normalize_decision_mode(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if value in {"policy", "agent"}:
        return value
    return "agent"


def _safe_float(raw: str | None, default: float) -> float:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _safe_int(raw: Any, default: int) -> int:
    try:
        return int(float(raw))
    except Exception:
        return int(default)


def _as_bool(raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def _check_api_key(service: ServiceState, x_api_key: str | None) -> None:
    if not service.api_key:
        return
    if (x_api_key or "") != service.api_key:
        raise HTTPException(status_code=403, detail="invalid_api_key")


def _event_with_channel_capabilities(*, event: IncomingEvent) -> IncomingEvent:
    raw = dict(event.raw or {})
    merged = _default_channel_capabilities(event=event)
    incoming = raw.get("channel_capabilities")
    if isinstance(incoming, dict):
        for key, val in incoming.items():
            if key in {"send_text", "send_image"}:
                merged[key] = bool(val)
            elif key in {"max_image_bytes"}:
                try:
                    iv = int(val)
                    if iv > 0:
                        merged[key] = iv
                except Exception:
                    pass
            elif key in {"send_image_input", "channel", "platform"}:
                text = str(val or "").strip()
                if text:
                    merged[key] = text
            else:
                merged[key] = val
    raw["channel_capabilities"] = merged
    return event.model_copy(update={"raw": raw})


def _default_channel_capabilities(event: IncomingEvent) -> dict[str, Any]:
    platform = (event.platform or "").strip().lower()
    if platform == "wecom":
        return {
            "channel": "wecom",
            "platform": "wecom",
            "send_text": True,
            "send_image": True,
            "send_image_input": "image_url",
            "max_image_bytes": 2 * 1024 * 1024,
        }
    if platform in {"douyin", "jinritemai"}:
        return {
            "channel": "worker",
            "platform": "douyin",
            "send_text": True,
            "send_image": True,
            "send_image_input": "image_url",
            "max_image_bytes": 8 * 1024 * 1024,
        }
    return {
        "channel": "worker",
        "platform": platform or "unknown",
        "send_text": True,
        "send_image": False,
        "send_image_input": "none",
    }


def _extract_channel_capabilities(event: IncomingEvent) -> dict[str, Any]:
    raw = event.raw if isinstance(event.raw, dict) else {}
    caps = raw.get("channel_capabilities")
    return dict(caps) if isinstance(caps, dict) else {}


def _render_event(event: IncomingEvent, *, image_input_mode: str = "none") -> str:
    text = (event.text or "").strip()
    raw = event.raw if isinstance(event.raw, dict) else {}
    media_path = str(raw.get("media_path") or "").strip()
    if str(event.message_type or "").strip().lower() == "image":
        if image_input_mode == "attachment":
            if text:
                return f"图片+文本：{text}，图片已作为原始附件提供"
            return "图片消息，图片已作为原始附件提供"
        if image_input_mode == "missing":
            if text:
                return f"图片+文本：{text}，但图片附件未成功传入"
            return "图片消息，但图片附件未成功传入"
        if image_input_mode == "summary":
            if text:
                return f"图片+文本：{text}，图片内容已通过文字摘要提供"
            return "图片消息，图片内容已通过文字摘要提供"
        if media_path and text:
            return f"图片+文本：{text}，image={media_path}"
        if media_path:
            return f"图片消息，image={media_path}"
        if text:
            return f"图片+文本：{text}，image=（无）"
        return "图片消息，image=（无）"
    return text or "（空）"
