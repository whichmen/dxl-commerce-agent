from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from . import __version__
from .models import now_ms
from .wecom_bridge import WeComBridge, WeComIncomingMessage


@dataclass
class WeComHubApp:
    app_key: str
    app_name: str
    decision_url: str
    decision_api_key: str
    fallback_text: str
    bridge: WeComBridge


class WeComHubState:
    def __init__(
        self,
        *,
        apps: dict[str, WeComHubApp],
        api_key: str,
        timeout_sec: float,
    ) -> None:
        self.apps = apps
        self.api_key = str(api_key or "").strip()
        self.timeout_sec = max(3.0, float(timeout_sec))
        self.started_at_ms = now_ms()


def _safe_text(raw: Any) -> str:
    return str(raw or "").strip()


def _safe_int(raw: Any, *, default: int) -> int:
    try:
        return int(raw)
    except Exception:
        return int(default)


def _coerce_recipients(raw: Any) -> str:
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        text = _safe_text(raw)
        if not text:
            return ""
        items = [item for item in text.replace("，", ",").replace("；", ";").split(",")]
        if len(items) == 1:
            items = text.replace("；", ";").replace("，", ",").replace(" ", ",").split(",")

    out: list[str] = []
    for item in items:
        value = _safe_text(item)
        if not value:
            continue
        if value == "@all":
            return "@all"
        if value not in out:
            out.append(value)
    return "|".join(out)


def _split_text_by_utf8_bytes(
    *,
    content: str,
    max_bytes: int,
    max_chunks: int,
) -> list[str]:
    text = _safe_text(content)
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


def _http_json(
    *,
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    timeout_sec: float,
    api_key: str,
) -> dict[str, Any]:
    body = None
    req = urllib_request.Request(url=url, method=method.upper())
    req.add_header("Accept", "application/json")
    if api_key:
        req.add_header("X-Api-Key", api_key)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req.data = body
        req.add_header("Content-Type", "application/json; charset=utf-8")

    try:
        with urllib_request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"http_{exc.code}:{raw[:200]}") from exc
    parsed = json.loads(raw or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _get_required_app(state: WeComHubState, app_key: str) -> WeComHubApp:
    key = _safe_text(app_key)
    app = state.apps.get(key)
    if app is None:
        raise HTTPException(status_code=404, detail="wecom_app_not_found")
    return app


def _check_api_key(state: WeComHubState, x_api_key: str | None) -> None:
    if state.api_key and (x_api_key or "") != state.api_key:
        raise HTTPException(status_code=403, detail="invalid_api_key")


def _coerce_msg_id(msg: WeComIncomingMessage) -> str:
    msg_id = _safe_text(msg.msg_id)
    if msg_id:
        return msg_id
    stable = "|".join(
        [
            _safe_text(msg.from_user),
            _safe_text(msg.to_user),
            _safe_text(msg.agent_id),
            _safe_text(msg.msg_type),
            _safe_text(msg.create_time),
            _safe_text(msg.content),
            _safe_text(msg.pic_url),
            _safe_text(msg.event),
            _safe_text(msg.event_key),
        ]
    )
    return "wecom_" + hashlib.sha1(stable.encode("utf-8")).hexdigest()[:24]


def _build_decision_payload(app: WeComHubApp, msg: WeComIncomingMessage) -> dict[str, Any]:
    message_type = "image" if msg.msg_type == "image" else "text"
    text = _safe_text(msg.content) if msg.msg_type == "text" else ""
    media_url = _safe_text(msg.pic_url) if msg.msg_type == "image" else ""
    return {
        "event": {
            "tenant_id": "default",
            "platform": "wecom",
            "store_id": f"wecom_app_{app.app_key}",
            "store_name": app.app_name,
            "role": "admin",
            "customer_id": _safe_text(msg.from_user),
            "platform_nickname": _safe_text(msg.from_user),
            "message_id": _coerce_msg_id(msg),
            "message_type": message_type,
            "text": text,
            "media_url": media_url,
            "raw": {
                "wecom_msg_type": _safe_text(msg.msg_type),
                "create_time": _safe_text(msg.create_time),
                "event": _safe_text(msg.event),
                "event_key": _safe_text(msg.event_key),
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
        }
    }


def _send_text_chunks(
    app: WeComHubApp,
    *,
    to_user: str,
    content: str,
    max_bytes: int = 1800,
    max_chunks: int = 8,
) -> tuple[bool, str]:
    chunks = _split_text_by_utf8_bytes(
        content=content,
        max_bytes=max(200, int(max_bytes)),
        max_chunks=max(1, int(max_chunks)),
    )
    if not chunks:
        return False, ""

    sent: list[str] = []
    for chunk in chunks:
        result = app.bridge.send_text(to_user=to_user, content=chunk)
        if not bool(result.get("ok")):
            return False, "\n".join(sent).strip()
        sent.append(chunk)
    return True, "\n".join(sent).strip()


def _send_decision_reply(app: WeComHubApp, *, to_user: str, payload: dict[str, Any]) -> dict[str, Any]:
    action = _safe_text(payload.get("action")).lower()
    reply_text = _safe_text(payload.get("reply_text"))
    image_urls = payload.get("reply_image_urls")
    images = [str(item).strip() for item in image_urls] if isinstance(image_urls, list) else []

    if action and action != "send":
        return {"ok": True, "message": "no_send_action", "data": {"action": action}}
    if (not reply_text) and (not images):
        reply_text = app.fallback_text

    sent_text = ""
    if reply_text:
        text_ok, sent_text = _send_text_chunks(app, to_user=to_user, content=reply_text)
        if not text_ok:
            return {"ok": False, "message": "wecom_send_text_failed", "data": {"reply_text": sent_text}}

    sent_images: list[str] = []
    for image_url in images:
        result = app.bridge.send_image_url(to_user=to_user, image_url=image_url)
        if not bool(result.get("ok")):
            return {
                "ok": False,
                "message": "wecom_send_image_failed",
                "data": {"sent_text": sent_text, "sent_images": sent_images, "image_url": image_url},
            }
        sent_images.append(image_url)

    return {
        "ok": True,
        "message": "ok",
        "data": {"sent_text": sent_text, "sent_images": sent_images},
    }


def _process_callback(state: WeComHubState, app: WeComHubApp, msg: WeComIncomingMessage) -> None:
    if msg.msg_type == "event":
        return
    try:
        decision = _http_json(
            method="POST",
            url=app.decision_url,
            payload=_build_decision_payload(app, msg),
            timeout_sec=state.timeout_sec,
            api_key=app.decision_api_key,
        )
    except Exception as exc:
        fallback = app.fallback_text or "稍等"
        _send_text_chunks(app, to_user=msg.from_user, content=fallback)
        print(
            f"[wecom-hub] decision_failed app={app.app_key} user={msg.from_user} err={type(exc).__name__}:{exc}",
            flush=True,
        )
        return

    result = _send_decision_reply(app, to_user=msg.from_user, payload=decision)
    if not bool(result.get("ok")):
        print(
            f"[wecom-hub] send_reply_failed app={app.app_key} user={msg.from_user} detail={result}",
            flush=True,
        )


def _load_apps(config_path: str, *, timeout_sec: float, fallback_text: str) -> dict[str, WeComHubApp]:
    with open(config_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw_apps = payload.get("apps")
    if not isinstance(raw_apps, dict) or (not raw_apps):
        raise RuntimeError("wecom_hub_apps_missing")

    apps: dict[str, WeComHubApp] = {}
    for raw_key, raw_value in raw_apps.items():
        if not isinstance(raw_value, dict):
            continue
        app_key = _safe_text(raw_key)
        corp_id = _safe_text(raw_value.get("corp_id"))
        agent_id = _safe_text(raw_value.get("agent_id"))
        app_secret = _safe_text(raw_value.get("app_secret"))
        token = _safe_text(raw_value.get("token"))
        aes_key = _safe_text(raw_value.get("aes_key"))
        app_name = _safe_text(raw_value.get("app_name")) or app_key
        decision_url = _safe_text(raw_value.get("decision_url"))
        decision_api_key = _safe_text(raw_value.get("api_key"))
        per_app_fallback = _safe_text(raw_value.get("fallback_text")) or fallback_text
        if not all([app_key, corp_id, agent_id, app_secret, token, aes_key, decision_url]):
            raise RuntimeError(f"wecom_hub_app_invalid:{app_key or 'unknown'}")
        apps[app_key] = WeComHubApp(
            app_key=app_key,
            app_name=app_name,
            decision_url=decision_url,
            decision_api_key=decision_api_key,
            fallback_text=per_app_fallback,
            bridge=WeComBridge(
                token=token,
                encoding_aes_key=aes_key,
                corp_id=corp_id,
                app_secret=app_secret,
                agent_id=agent_id,
                timeout_sec=timeout_sec,
            ),
        )
    if not apps:
        raise RuntimeError("wecom_hub_apps_empty")
    return apps


def create_wecom_hub_app() -> FastAPI:
    config_path = _safe_text(
        os.getenv(
            "CLAWBOT_WECOM_HUB_CONFIG",
            "/etc/openclaw-wecom-hub.apps.json",
        )
    )
    timeout_sec = max(3.0, float(os.getenv("CLAWBOT_WECOM_HUB_TIMEOUT_SEC", "20") or "20"))
    fallback_text = _safe_text(os.getenv("CLAWBOT_WECOM_HUB_FALLBACK_TEXT", "稍等")) or "稍等"
    state = WeComHubState(
        apps=_load_apps(config_path, timeout_sec=timeout_sec, fallback_text=fallback_text),
        api_key=_safe_text(os.getenv("CLAWBOT_WECOM_HUB_API_KEY", "")),
        timeout_sec=timeout_sec,
    )

    app = FastAPI(title="openclaw-wecom-hub", version=__version__)
    app.state.service = state

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"ok": True, "service": "openclaw-wecom-hub", "version": __version__}

    @app.get("/health")
    def health() -> dict[str, Any]:
        service: WeComHubState = app.state.service
        return {
            "ok": True,
            "service": "openclaw-wecom-hub",
            "version": __version__,
            "uptime_ms": now_ms() - service.started_at_ms,
            "apps": sorted(service.apps.keys()),
        }

    @app.post("/v1/wecom/send")
    async def wecom_send(
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        service: WeComHubState = app.state.service
        _check_api_key(service, x_api_key)
        payload = await request.json()
        payload_map = payload if isinstance(payload, dict) else {}
        args = payload_map.get("args")
        event = payload_map.get("event")
        args_map = dict(args) if isinstance(args, dict) else dict(payload_map)
        event_map = dict(event) if isinstance(event, dict) else {}

        app_key = _safe_text(args_map.get("app_key") or payload_map.get("app_key"))
        if not app_key:
            raise HTTPException(status_code=400, detail="missing_app_key")
        app_cfg = _get_required_app(service, app_key)

        to_user = _coerce_recipients(
            args_map.get("touser")
            or args_map.get("to_user")
            or args_map.get("user_id")
            or event_map.get("touser")
            or event_map.get("to_user")
        )
        if not to_user:
            raise HTTPException(status_code=400, detail="missing_touser")

        msg_type = _safe_text(args_map.get("msg_type") or args_map.get("message_type") or "text").lower()
        if msg_type == "image":
            image_url = _safe_text(
                args_map.get("image_url")
                or args_map.get("media_url")
                or event_map.get("media_url")
            )
            if not image_url:
                raise HTTPException(status_code=400, detail="missing_image_url")
            result = app_cfg.bridge.send_image_url(to_user=to_user, image_url=image_url)
            return {
                "ok": bool(result.get("ok")),
                "message": _safe_text(result.get("message")) or "wecom_send_image_failed",
                "data": {"touser": to_user, "msg_type": "image", "image_url": image_url},
            }

        content = _safe_text(
            args_map.get("content")
            or args_map.get("text")
            or args_map.get("message")
            or event_map.get("text")
        )
        max_bytes = max(200, min(_safe_int(args_map.get("max_bytes"), default=1800), 3000))
        max_chunks = max(1, min(_safe_int(args_map.get("max_chunks"), default=8), 20))
        ok, sent_text = _send_text_chunks(
            app_cfg,
            to_user=to_user,
            content=content,
            max_bytes=max_bytes,
            max_chunks=max_chunks,
        )
        return {
            "ok": ok,
            "message": "ok" if ok else "wecom_send_text_failed",
            "data": {
                "touser": to_user,
                "msg_type": "text",
                "content": sent_text,
                "app_key": app_key,
            },
        }

    @app.get("/v1/wecom/{app_key}/callback")
    def wecom_verify(
        app_key: str,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> PlainTextResponse:
        service: WeComHubState = app.state.service
        app_cfg = _get_required_app(service, app_key)
        try:
            plain = app_cfg.bridge.verify_url(
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                echostr=echostr,
            )
        except Exception as exc:
            raise HTTPException(status_code=403, detail=f"wecom_verify_failed:{type(exc).__name__}") from exc
        return PlainTextResponse(plain)

    @app.post("/v1/wecom/{app_key}/callback")
    async def wecom_callback(
        app_key: str,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> PlainTextResponse:
        service: WeComHubState = app.state.service
        app_cfg = _get_required_app(service, app_key)
        msg_signature = _safe_text(request.query_params.get("msg_signature"))
        timestamp = _safe_text(request.query_params.get("timestamp"))
        nonce = _safe_text(request.query_params.get("nonce"))
        if not msg_signature or not timestamp or not nonce:
            raise HTTPException(status_code=400, detail="wecom_callback_query_missing")

        body = await request.body()
        body_xml = body.decode("utf-8", errors="ignore")
        try:
            msg = app_cfg.bridge.parse_callback_message(
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                body_xml=body_xml,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"wecom_callback_decode_failed:{type(exc).__name__}") from exc

        if (not app_cfg.bridge.should_process(msg)) or (not app_cfg.bridge.mark_once(msg)):
            return PlainTextResponse("success")

        background_tasks.add_task(_process_callback, service, app_cfg, msg)
        return PlainTextResponse("success")

    return app
