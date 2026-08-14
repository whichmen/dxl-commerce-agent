#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Weixin Channels (视频号) worker, v2 architecture.

Architecture:
1) ingest: API-based incremental ingest via /shop/commkf/* (browser cookies), no DOM discovery.
2) mailbox: append-only inbound events + durable per-chat ordered turn queue.
3) sender: API send by room_id + explicit send confirmation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import parse as urllib_parse

from playwright.async_api import (
    Browser,
    Page,
    Playwright,
    async_playwright,
)

try:
    from .core import DecisionClient, IncomingMessage, LocalStateStore, now_ms
    from .core.worker_health import (
        CATEGORY_FATAL_LOCAL_STATE,
        CATEGORY_MANUAL_ACTION,
        WorkerFatalError,
        WorkerHealthReporter,
        classify_worker_exception,
    )
except ImportError:  # Direct script execution from workers/
    from core import DecisionClient, IncomingMessage, LocalStateStore, now_ms
    from core.worker_health import (
        CATEGORY_FATAL_LOCAL_STATE,
        CATEGORY_MANUAL_ACTION,
        WorkerFatalError,
        WorkerHealthReporter,
        classify_worker_exception,
    )

DEFAULT_PORTS = (9224,)
DEFAULT_STORE_ID = os.environ.get("STORE_ID", "weixin_store_example")
DEFAULT_STORE_NAME = os.environ.get("STORE_NAME", "示例视频号店铺")
DEFAULT_DECISION_URL = os.environ.get("DECISION_URL", "http://127.0.0.1:18080")
DEFAULT_DECISION_KEY = os.environ.get("DECISION_KEY", "")
DEFAULT_DECISION_TIMEOUT_SEC = 75.0
DEFAULT_STATE_DB = str((Path(__file__).resolve().parent / "state" / "weixin_worker.db"))
DEFAULT_FALLBACK_REPLY = "稍等"
DEFAULT_VOICE_FALLBACK_REPLY = "这边暂时无法自动识别语音，为了尽快帮您处理，麻烦您发文字描述一下，或把订单/商品截图发来。"

DEFAULT_INGEST_POLL_MS = 350
DEFAULT_ROOM_MSG_LIMIT = 50
DEFAULT_MAX_DISPATCH_PER_LOOP = 8
DEFAULT_MEDIA_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_FIRST_REPLY_DEADLINE_MS = 8000
WEIXIN_KF_URL = "https://store.weixin.qq.com/shop/kf"

PLATFORM = "weixin"

CHANNEL_CAPABILITIES = {
    "channel": "worker",
    "platform": PLATFORM,
    "send_text": True,
    "send_image": False,
    "send_image_input": "none",
}

MSG_TYPE_MAP = {
    1: "text",
    2: "image",
    3: "image",
    4: "image",
    5: "system",
    6: "text",
    7: "text",
    8: "text",
    9: "voice",
    10: "video",
}
ORDER_CARD_MSG_TYPE = "order_card"
LOGIN_PAGE_MARKERS = (
    "扫码进入我的小店",
    "扫码进入小店",
)


@dataclass
class TurnEvent:
    id: int
    sync_id: int
    message_id: str
    message_type: str
    text: str
    media_url: str
    raw_json: str
    event_ts_ms: int


@dataclass
class PendingTurn:
    turn_id: int
    lock_token: str
    chat_id: str
    room_id: str
    customer_id: str
    customer_openid: str
    platform_nickname: str
    session_key: str
    first_sync_id: int
    last_sync_id: int
    events: List[TurnEvent]

    @property
    def event_ids(self) -> List[int]:
        return [e.id for e in self.events]


def _log(msg: str) -> None:
    print(msg, flush=True)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        text = str(value).strip()
        if not text:
            return default
        return int(text)
    except Exception:
        return default


def _normalize_nickname(value: str) -> str:
    text = (value or "").replace("\u200b", "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*下单数\(\d+\)\s*$", "", text).strip()
    return text[:64]


def _extract_order_id_from_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return ""
    patterns = (
        r"(?:订单号|订单编号|平台单号)\s*[:：]?\s*([A-Za-z0-9_-]{8,})",
        r"\b([A-Za-z][0-9]{12,}|[0-9]{12,}|[0-9]{6,}-[0-9]{6,})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, value)
        if m:
            return (m.group(1) or "").strip()
    return ""


def _build_customer_id(chat_id: str, seed: str) -> str:
    base = chat_id or seed or "weixin_customer"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


def _score_page_url(url: str) -> int:
    if not url:
        return 0
    try:
        parsed = urllib_parse.urlsplit(url)
    except Exception:
        return 0
    if parsed.netloc != "store.weixin.qq.com":
        return 0
    path = parsed.path.rstrip("/")
    if path == "/shop/kf":
        return 120
    if path == "/shop":
        query = urllib_parse.parse_qs(parsed.query)
        redirect_values = query.get("redirect_url") or query.get("redirectUrl") or []
        if any(str(value).rstrip("/") == "/kf" for value in redirect_values):
            return 100
    return 0


def _looks_like_login_page(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or ""))
    if not normalized:
        return False
    if any(marker in normalized for marker in LOGIN_PAGE_MARKERS):
        return True
    return "扫码" in normalized and "我的小店" in normalized


async def _detect_login_page_detail(page: Page) -> str:
    try:
        snapshot = await page.evaluate(
            """() => {
                const body = document.body ? (document.body.innerText || '') : '';
                return {
                    title: document.title || '',
                    body: body.slice(0, 2400),
                    url: location.href || '',
                  };
            }"""
        )
    except Exception:
        return ""

    if not isinstance(snapshot, dict):
        return ""

    title = str(snapshot.get("title") or "").strip()
    body = str(snapshot.get("body") or "").strip()
    page_url = str(snapshot.get("url") or "").strip()
    combined = "\n".join(part for part in (title, body) if part).strip()
    if not _looks_like_login_page(combined):
        return ""

    detail = "页面显示扫码进入我的小店，需要重新登录"
    if page_url and "store.weixin.qq.com" not in page_url:
        detail = f"{detail} ({page_url[:120]})"
    return detail


def _parse_json_dict(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_media_urls(content_obj: Dict[str, Any]) -> List[str]:
    if not content_obj:
        return []

    def _pick(obj: Any) -> str:
        return obj.strip() if isinstance(obj, str) and obj.strip() else ""

    urls: List[str] = []

    def _push(raw: Any) -> None:
        value = _pick(raw)
        if value and value not in urls:
            urls.append(value)

    # Prefer higher-quality image links first: full -> original -> high -> low.
    nested_paths = (
        ("full_img", "img_uri", "url"),
        ("full_img", "url"),
        ("ori_img", "img_uri", "url"),
        ("ori_img", "url"),
        ("ori_img_uri", "url"),
        ("origin_img_uri", "url"),
        ("hd_img", "img_uri", "url"),
        ("hd_img", "url"),
        ("ld_img", "img_uri", "url"),
        ("ld_img", "url"),
        ("img_uri", "url"),
        ("image", "url"),
        ("image", "image_url"),
        ("image", "img_url"),
        ("video", "url"),
        ("voice", "url"),
    )
    for path in nested_paths:
        cur: Any = content_obj
        ok = True
        for key in path:
            if not isinstance(cur, dict):
                ok = False
                break
            cur = cur.get(key)
        if not ok:
            continue
        _push(cur)

    for key in ("media_url", "image_url", "img_url", "url", "thumb_url"):
        _push(content_obj.get(key))

    return urls


def _extract_media_url(content_obj: Dict[str, Any]) -> str:
    urls = _extract_media_urls(content_obj)
    return urls[0] if urls else ""


def _extract_media_url_from_text_blob(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    # Payloads may include escaped JSON text blobs.
    normalized = raw.replace("\\/", "/")
    m = re.search(r"https?://[^\s\"'}]+", normalized)
    if not m:
        return ""
    return str(m.group(0) or "").strip()


def _extract_order_card_media_url(card_obj: Dict[str, Any]) -> str:
    if not card_obj:
        return ""
    for key in ("product_image_url", "product_img_url", "image_url", "img_url", "thumb_url"):
        value = str(card_obj.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _build_order_card_text(card_obj: Dict[str, Any]) -> str:
    if not card_obj:
        return "[订单卡片]"
    order_id = str(card_obj.get("order_id") or "").strip()
    products = str(card_obj.get("products") or "").strip()
    price = str(card_obj.get("price_wording") or "").strip()
    state = str(card_obj.get("state_wording") or "").strip()

    lines = ["[订单卡片]"]
    if order_id:
        lines.append(f"订单号: {order_id}")
    if products:
        lines.append(f"商品: {products}")
    if price:
        lines.append(f"金额: {price}")
    if state:
        lines.append(f"状态: {state}")
    return "\n".join(lines)


def _looks_like_voice_payload(payload: Dict[str, Any]) -> bool:
    if not payload:
        return False
    if _to_int(payload.get("voice_size")) > 0:
        return True
    if _to_int(payload.get("play_length")) > 0 and any(
        str(payload.get(key) or "").strip()
        for key in ("mp3_voice_buf", "voice_buf", "voice_url", "mp3_voice_url")
    ):
        return True
    if any(
        str(payload.get(key) or "").strip()
        for key in ("mp3_voice_buf", "voice_buf", "voice_url", "mp3_voice_url")
    ):
        return True
    return False


def _map_message_type(msg_type_code: int, media_url: str) -> str:
    mapped = MSG_TYPE_MAP.get(msg_type_code, "text")
    if mapped == "text" and media_url:
        return "image"
    return mapped


def _normalize_customer_inbound_event(
    raw: Dict[str, Any],
    *,
    kf_openid: str,
    store_id: str,
    fallback_nickname: str,
) -> Optional[Dict[str, Any]]:
    direction = _to_int(raw.get("msg_direction"))
    if direction != 1:
        return None

    if _to_int(raw.get("is_delete")) == 1:
        return None
    if _to_int(raw.get("is_revoked")) == 1:
        return None

    room_id = str(raw.get("room_id") or "").strip()
    if not room_id:
        return None

    message_id = str(raw.get("msg_id") or "").strip()
    if not message_id:
        return None

    send_openid = str(raw.get("send_openid") or "").strip()

    msg_type_code_raw = _to_int(raw.get("msg_type"), 1)
    content_obj = _parse_json_dict(raw.get("msg_kf_content"))
    msg_type_code = _to_int(content_obj.get("type"), msg_type_code_raw) if content_obj else msg_type_code_raw
    text = str(content_obj.get("content") or "").strip() if content_obj else ""
    media_url = _extract_media_url(content_obj)
    order_card_obj: Dict[str, Any] = {}
    nested_obj: Dict[str, Any] = {}

    # Some Weixin payloads nest media fields inside content JSON strings.
    if text and text[:1] in {"{", "["}:
        nested = _parse_json_dict(text)
        if nested:
            nested_obj = nested
            if msg_type_code == 14:
                order_card_obj = nested
            nested_media = _extract_media_url(nested)
            if nested_media and not media_url:
                media_url = nested_media
            nested_text = str(nested.get("content") or "").strip()
            if nested_text and nested_text != text:
                text = nested_text
            elif nested_media:
                text = ""

    if not media_url and text and msg_type_code in {2, 3, 4, 9, 10}:
        media_url = _extract_media_url_from_text_blob(text)

    if msg_type_code == 14:
        if not media_url:
            media_url = _extract_order_card_media_url(order_card_obj)
        text = _build_order_card_text(order_card_obj)
        message_type = ORDER_CARD_MSG_TYPE
    elif _looks_like_voice_payload(content_obj) or _looks_like_voice_payload(nested_obj):
        message_type = "voice"
    else:
        message_type = _map_message_type(msg_type_code, media_url)

    if message_type in {"image", "video", "voice"} and text[:1] in {"{", "["}:
        text = ""

    if message_type == "system":
        return None

    if (not text) and (not media_url):
        if message_type == "image":
            text = "[图片]"
        elif message_type == "video":
            text = "[视频]"
        elif message_type == "voice":
            text = "[语音]"
        else:
            return None

    event_ts_ms = _to_int(raw.get("create_time_ms"))
    if event_ts_ms <= 0:
        create_sec = _to_int(raw.get("create_time"))
        if create_sec > 0:
            event_ts_ms = create_sec * 1000
        else:
            event_ts_ms = now_ms()

    nickname = _normalize_nickname(fallback_nickname)
    if not nickname:
        if send_openid:
            nickname = f"user_{send_openid[-6:]}"
        else:
            nickname = f"room_{room_id[-6:]}"

    customer_id = _build_customer_id(room_id, send_openid or nickname)
    session_key = f"{PLATFORM}:{store_id}:{nickname}"
    order_id = _extract_order_id_from_text(text)

    return {
        "chat_id": room_id,
        "room_id": room_id,
        "source_ref": room_id,
        "customer_id": customer_id,
        "customer_openid": send_openid,
        "platform_nickname": nickname,
        "session_key": session_key,
        "sync_id": _to_int(raw.get("id")),
        "message_id": message_id,
        "message_type": message_type,
        "text": text,
        "media_url": media_url,
        "order_id": order_id,
        "event_ts_ms": event_ts_ms,
        "raw_json": json.dumps(raw, ensure_ascii=False)[:12000],
    }


class WeixinEventStore:
    """Durable mailbox queue + outbound turns."""

    TBL_CHAT = "weixin_v2_chat_cursor"
    TBL_BASELINE = "weixin_v2_chat_baseline"
    TBL_INBOUND = "weixin_v2_inbound_events"
    TBL_TURN = "weixin_v2_outbound_turns"

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=8.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TBL_CHAT} (
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    room_id TEXT NOT NULL DEFAULT '',
                    customer_openid TEXT NOT NULL DEFAULT '',
                    platform_nickname TEXT NOT NULL DEFAULT '',
                    last_seen_sync_id INTEGER NOT NULL DEFAULT 0,
                    last_replied_sync_id INTEGER NOT NULL DEFAULT 0,
                    sla_window_start_ms INTEGER NOT NULL DEFAULT 0,
                    sla_placeholder_sent_ms INTEGER NOT NULL DEFAULT 0,
                    sync_status TEXT NOT NULL DEFAULT 'ok',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, platform, store_id, chat_id)
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TBL_CHAT}_sync
                ON {self.TBL_CHAT} (tenant_id, platform, store_id, sync_status, updated_at_ms)
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TBL_BASELINE} (
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    baseline_message_id TEXT NOT NULL DEFAULT '',
                    baseline_event_ts_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, platform, store_id, chat_id)
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TBL_INBOUND} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    room_id TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT '',
                    customer_id TEXT NOT NULL,
                    customer_openid TEXT NOT NULL DEFAULT '',
                    platform_nickname TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    sync_id INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    media_url TEXT NOT NULL DEFAULT '',
                    order_id TEXT NOT NULL DEFAULT '',
                    event_ts_ms INTEGER NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{{}}',
                    created_at_ms INTEGER NOT NULL,
                    available_at_ms INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    turn_id INTEGER NOT NULL DEFAULT 0,
                    lock_token TEXT NOT NULL DEFAULT '',
                    locked_at_ms INTEGER NOT NULL DEFAULT 0,
                    decision_action TEXT NOT NULL DEFAULT '',
                    decision_reason TEXT NOT NULL DEFAULT '',
                    reply_text TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at_ms INTEGER NOT NULL,
                    UNIQUE(tenant_id, platform, store_id, chat_id, message_id)
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TBL_INBOUND}_claim
                ON {self.TBL_INBOUND} (status, available_at_ms, sync_id, id)
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TBL_INBOUND}_chat
                ON {self.TBL_INBOUND} (tenant_id, platform, store_id, chat_id, status, sync_id, id)
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TBL_TURN} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    room_id TEXT NOT NULL DEFAULT '',
                    customer_id TEXT NOT NULL,
                    customer_openid TEXT NOT NULL DEFAULT '',
                    platform_nickname TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    first_sync_id INTEGER NOT NULL,
                    last_sync_id INTEGER NOT NULL,
                    first_message_id TEXT NOT NULL,
                    last_message_id TEXT NOT NULL,
                    event_count INTEGER NOT NULL,
                    request_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'processing',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    lock_token TEXT NOT NULL DEFAULT '',
                    decision_action TEXT NOT NULL DEFAULT '',
                    decision_reason TEXT NOT NULL DEFAULT '',
                    reply_text TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TBL_TURN}_status
                ON {self.TBL_TURN} (status, updated_at_ms, id)
                """
            )
            self._ensure_column_conn(
                conn,
                table=self.TBL_CHAT,
                column="sla_window_start_ms",
                column_def="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column_conn(
                conn,
                table=self.TBL_CHAT,
                column="sla_placeholder_sent_ms",
                column_def="INTEGER NOT NULL DEFAULT 0",
            )

    def _ensure_column_conn(
        self,
        conn: sqlite3.Connection,
        *,
        table: str,
        column: str,
        column_def: str,
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        exists = any(str(row[1] or "").strip() == column for row in rows)
        if exists:
            return
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")

    def _roll_chat_sla_window_on_new_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        at_ms: int,
    ) -> None:
        # A-scheme: first-reply timer starts when message enters the local queue,
        # not from upstream event_ts.
        start_ts = max(0, int(at_ms))
        if start_ts <= 0:
            start_ts = now_ms()
        conn.execute(
            f"""
            UPDATE {self.TBL_CHAT}
            SET sla_window_start_ms=CASE
                    WHEN sla_window_start_ms <= 0 THEN ?
                    ELSE sla_window_start_ms
                END,
                sla_placeholder_sent_ms=CASE
                    WHEN sla_window_start_ms <= 0 THEN 0
                    ELSE sla_placeholder_sent_ms
                END,
                updated_at_ms=?
            WHERE tenant_id=? AND platform=? AND store_id=? AND chat_id=?
            """,
            (
                start_ts,
                at_ms,
                tenant_id,
                PLATFORM,
                store_id,
                chat_id,
            ),
        )

    def _upsert_chat_cursor_conn(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        room_id: str,
        customer_openid: str,
        platform_nickname: str,
        seen_sync_id: int = -1,
        replied_sync_id: int = -1,
        sync_status: str = "",
        last_error: str = "",
        at_ms: int,
    ) -> None:
        sync_status_raw = (sync_status or "").strip()
        sync_status_specified = 1 if sync_status_raw else 0
        sync_status_value = sync_status_raw if sync_status_specified else "ok"
        last_error_raw = (last_error or "").strip()
        last_error_specified = 1 if last_error_raw else 0
        conn.execute(
            f"""
            INSERT INTO {self.TBL_CHAT} (
                tenant_id, platform, store_id, chat_id,
                room_id, customer_openid, platform_nickname,
                last_seen_sync_id, last_replied_sync_id,
                sync_status, last_error,
                updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, platform, store_id, chat_id)
            DO UPDATE SET
                room_id=CASE
                    WHEN excluded.room_id != '' THEN excluded.room_id
                    ELSE {self.TBL_CHAT}.room_id
                END,
                customer_openid=CASE
                    WHEN excluded.customer_openid != '' THEN excluded.customer_openid
                    ELSE {self.TBL_CHAT}.customer_openid
                END,
                platform_nickname=CASE
                    WHEN excluded.platform_nickname != '' THEN excluded.platform_nickname
                    ELSE {self.TBL_CHAT}.platform_nickname
                END,
                last_seen_sync_id=CASE
                    WHEN excluded.last_seen_sync_id >= 0
                         AND excluded.last_seen_sync_id > {self.TBL_CHAT}.last_seen_sync_id
                    THEN excluded.last_seen_sync_id
                    ELSE {self.TBL_CHAT}.last_seen_sync_id
                END,
                last_replied_sync_id=CASE
                    WHEN excluded.last_replied_sync_id >= 0
                         AND excluded.last_replied_sync_id > {self.TBL_CHAT}.last_replied_sync_id
                    THEN excluded.last_replied_sync_id
                    ELSE {self.TBL_CHAT}.last_replied_sync_id
                END,
                sync_status=CASE
                    WHEN ? = 1 THEN excluded.sync_status
                    ELSE {self.TBL_CHAT}.sync_status
                END,
                last_error=CASE
                    WHEN ? = 1 THEN excluded.last_error
                    ELSE {self.TBL_CHAT}.last_error
                END,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                tenant_id,
                PLATFORM,
                store_id,
                chat_id,
                room_id,
                customer_openid,
                platform_nickname,
                int(seen_sync_id),
                int(replied_sync_id),
                sync_status_value[:16],
                last_error_raw[:300],
                at_ms,
                sync_status_specified,
                last_error_specified,
            ),
        )

    def upsert_chat_cursor(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        room_id: str,
        customer_openid: str,
        platform_nickname: str,
        seen_sync_id: int = -1,
        replied_sync_id: int = -1,
        sync_status: str = "",
        last_error: str = "",
    ) -> None:
        with self._connect() as conn:
            self._upsert_chat_cursor_conn(
                conn,
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=chat_id,
                room_id=room_id,
                customer_openid=customer_openid,
                platform_nickname=platform_nickname,
                seen_sync_id=seen_sync_id,
                replied_sync_id=replied_sync_id,
                sync_status=sync_status,
                last_error=last_error,
                at_ms=now_ms(),
            )

    def load_chat_cache(self, *, tenant_id: str, store_id: str) -> Dict[str, Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT chat_id, customer_openid, platform_nickname
                FROM {self.TBL_CHAT}
                WHERE tenant_id=? AND platform=? AND store_id=?
                """,
                (tenant_id, PLATFORM, store_id),
            ).fetchall()
        cache: Dict[str, Dict[str, str]] = {}
        for row in rows:
            chat_id = str(row["chat_id"] or "").strip()
            if not chat_id:
                continue
            cache[chat_id] = {
                "nickname": str(row["platform_nickname"] or "").strip(),
                "customer_openid": str(row["customer_openid"] or "").strip(),
            }
        return cache

    def get_chat_baseline(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
    ) -> Tuple[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT baseline_message_id, baseline_event_ts_ms
                FROM {self.TBL_BASELINE}
                WHERE tenant_id=? AND platform=? AND store_id=? AND chat_id=?
                """,
                (tenant_id, PLATFORM, store_id, chat_id),
            ).fetchone()
        if not row:
            return "", 0
        return str(row["baseline_message_id"] or "").strip(), _to_int(row["baseline_event_ts_ms"], 0)

    def set_chat_baseline(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        baseline_message_id: str,
        baseline_event_ts_ms: int,
        at_ms: int,
    ) -> None:
        msg_id = str(baseline_message_id or "").strip()
        if not msg_id:
            return
        ts = max(0, int(at_ms))
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self.TBL_BASELINE} (
                    tenant_id, platform, store_id, chat_id,
                    baseline_message_id, baseline_event_ts_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, platform, store_id, chat_id)
                DO UPDATE SET
                    baseline_message_id=excluded.baseline_message_id,
                    baseline_event_ts_ms=excluded.baseline_event_ts_ms,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    tenant_id,
                    PLATFORM,
                    store_id,
                    chat_id,
                    msg_id,
                    max(0, int(baseline_event_ts_ms)),
                    ts,
                ),
            )

    def prune_chat_baseline_not_in(
        self,
        *,
        tenant_id: str,
        store_id: str,
        keep_chat_ids: Iterable[str],
    ) -> None:
        keep = [str(item or "").strip() for item in keep_chat_ids]
        keep = [item for item in keep if item]
        with self._connect() as conn:
            if keep:
                placeholders = ",".join("?" for _ in keep)
                conn.execute(
                    f"""
                    DELETE FROM {self.TBL_BASELINE}
                    WHERE tenant_id=? AND platform=? AND store_id=?
                      AND chat_id NOT IN ({placeholders})
                    """,
                    (tenant_id, PLATFORM, store_id, *keep),
                )
            else:
                conn.execute(
                    f"""
                    DELETE FROM {self.TBL_BASELINE}
                    WHERE tenant_id=? AND platform=? AND store_id=?
                    """,
                    (tenant_id, PLATFORM, store_id),
                )

    def get_chat_sla_state(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
    ) -> Tuple[int, int]:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT sla_window_start_ms, sla_placeholder_sent_ms
                FROM {self.TBL_CHAT}
                WHERE tenant_id=? AND platform=? AND store_id=? AND chat_id=?
                """,
                (tenant_id, PLATFORM, store_id, chat_id),
            ).fetchone()
        if not row:
            return 0, 0
        return _to_int(row["sla_window_start_ms"], 0), _to_int(row["sla_placeholder_sent_ms"], 0)

    def mark_chat_sla_placeholder_sent(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        at_ms: int,
    ) -> None:
        ts = max(0, int(at_ms))
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {self.TBL_CHAT}
                SET sla_window_start_ms=0,
                    sla_placeholder_sent_ms=CASE
                        WHEN sla_placeholder_sent_ms > 0 THEN sla_placeholder_sent_ms
                        ELSE ?
                    END,
                    updated_at_ms=?
                WHERE tenant_id=? AND platform=? AND store_id=? AND chat_id=?
                """,
                (ts, ts, tenant_id, PLATFORM, store_id, chat_id),
            )

    def clear_chat_sla_window(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        at_ms: int,
    ) -> None:
        ts = max(0, int(at_ms))
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {self.TBL_CHAT}
                SET sla_window_start_ms=0,
                    sla_placeholder_sent_ms=0,
                    updated_at_ms=?
                WHERE tenant_id=? AND platform=? AND store_id=? AND chat_id=?
                """,
                (ts, tenant_id, PLATFORM, store_id, chat_id),
            )

    def ingest_room_compensate_events(
        self,
        *,
        tenant_id: str,
        store_id: str,
        events: List[Dict[str, Any]],
        anchor_sync_id: int,
    ) -> int:
        if not events:
            return 0

        inserted = 0
        now_ts = now_ms()
        synthetic_sync_id = max(1, int(anchor_sync_id))

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for event_payload in events:
                chat_id = str(event_payload.get("chat_id") or "").strip()
                if not chat_id:
                    continue
                room_id = str(event_payload.get("room_id") or "").strip() or chat_id

                self._upsert_chat_cursor_conn(
                    conn,
                    tenant_id=tenant_id,
                    store_id=store_id,
                    chat_id=chat_id,
                    room_id=room_id,
                    customer_openid=str(event_payload.get("customer_openid") or ""),
                    platform_nickname=str(event_payload.get("platform_nickname") or ""),
                    seen_sync_id=synthetic_sync_id,
                    replied_sync_id=-1,
                    at_ms=now_ts,
                )

                ok = self._insert_inbound_event_conn(
                    conn,
                    tenant_id=tenant_id,
                    store_id=store_id,
                    event_payload=event_payload,
                    sync_id=synthetic_sync_id,
                    available_at_ms=now_ts,
                    created_at_ms=now_ts,
                    updated_at_ms=now_ts,
                )
                if not ok:
                    continue

                inserted += 1
                conn.execute(
                    f"""
                    UPDATE {self.TBL_INBOUND}
                    SET available_at_ms=CASE
                            WHEN available_at_ms < ? THEN ?
                            ELSE available_at_ms
                        END,
                        updated_at_ms=?
                    WHERE tenant_id=? AND platform=? AND store_id=? AND chat_id=? AND status='new'
                    """,
                    (
                        now_ts,
                        now_ts,
                        now_ts,
                        tenant_id,
                        PLATFORM,
                        store_id,
                        chat_id,
                    ),
                )
            conn.commit()

        return inserted

    def _insert_inbound_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        store_id: str,
        event_payload: Dict[str, Any],
        sync_id: int,
        available_at_ms: int,
        created_at_ms: int,
        updated_at_ms: int,
    ) -> bool:
        try:
            conn.execute(
                f"""
                INSERT INTO {self.TBL_INBOUND} (
                    tenant_id, platform, store_id,
                    chat_id, room_id, source_ref,
                    customer_id, customer_openid, platform_nickname, session_key,
                    sync_id, message_id, message_type,
                    text, media_url, order_id,
                    event_ts_ms, raw_json,
                    created_at_ms, available_at_ms,
                    status, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
                """,
                (
                    tenant_id,
                    PLATFORM,
                    store_id,
                    event_payload["chat_id"],
                    event_payload["room_id"],
                    event_payload["source_ref"],
                    event_payload["customer_id"],
                    event_payload["customer_openid"],
                    event_payload["platform_nickname"],
                    event_payload["session_key"],
                    int(sync_id),
                    event_payload["message_id"],
                    event_payload["message_type"],
                    event_payload["text"],
                    event_payload["media_url"],
                    event_payload["order_id"],
                    int(event_payload["event_ts_ms"]),
                    event_payload["raw_json"],
                    int(created_at_ms),
                    int(available_at_ms),
                    int(updated_at_ms),
                ),
            )
            self._upsert_chat_cursor_conn(
                conn,
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=event_payload["chat_id"],
                room_id=event_payload["room_id"],
                customer_openid=event_payload["customer_openid"],
                platform_nickname=event_payload["platform_nickname"],
                seen_sync_id=-1,
                replied_sync_id=-1,
                at_ms=int(updated_at_ms),
            )
            self._roll_chat_sla_window_on_new_event_conn(
                conn,
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=event_payload["chat_id"],
                at_ms=int(updated_at_ms),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def claim_next_turn(
        self,
        *,
        tenant_id: str,
        store_id: str,
        now_ts_ms: int,
        exclude_chat_ids: Optional[Iterable[str]] = None,
    ) -> Optional[PendingTurn]:
        blocked_chat_ids = [str(item or "").strip() for item in (exclude_chat_ids or [])]
        blocked_chat_ids = [item for item in blocked_chat_ids if item]

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            blocked_sql = ""
            params: List[Any] = [tenant_id, PLATFORM, store_id, now_ts_ms]
            if blocked_chat_ids:
                blocked_sql = f" AND e.chat_id NOT IN ({','.join('?' for _ in blocked_chat_ids)})"
                params.extend(blocked_chat_ids)

            head = conn.execute(
                f"""
                SELECT e.*
                FROM {self.TBL_INBOUND} e
                WHERE e.tenant_id=? AND e.platform=? AND e.store_id=?
                  AND e.status='new'
                  AND e.available_at_ms <= ?
                  {blocked_sql}
                ORDER BY e.sync_id ASC, e.id ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()

            if head is None:
                conn.commit()
                return None

            chat_id = str(head["chat_id"] or "")
            event_rows = conn.execute(
                f"""
                SELECT *
                FROM {self.TBL_INBOUND}
                WHERE tenant_id=? AND platform=? AND store_id=?
                  AND id=?
                  AND status='new'
                  AND available_at_ms <= ?
                LIMIT 1
                """,
                (
                    tenant_id,
                    PLATFORM,
                    store_id,
                    int(head["id"]),
                    now_ts_ms,
                ),
            ).fetchall()

            if not event_rows:
                conn.commit()
                return None

            event_ids = [int(row["id"]) for row in event_rows]
            first = event_rows[0]
            last = event_rows[-1]
            lock_token = hashlib.sha1(
                f"turn:{chat_id}:{first['sync_id']}:{now_ts_ms}".encode("utf-8")
            ).hexdigest()[:20]

            request_text = "\n".join(
                [str(row["text"] or "").strip() for row in event_rows if str(row["text"] or "").strip()]
            )

            turn_cur = conn.execute(
                f"""
                INSERT INTO {self.TBL_TURN} (
                    tenant_id, platform, store_id,
                    chat_id, room_id,
                    customer_id, customer_openid, platform_nickname, session_key,
                    first_sync_id, last_sync_id,
                    first_message_id, last_message_id,
                    event_count, request_text,
                    status, attempts, lock_token,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', 1, ?, ?, ?)
                """,
                (
                    tenant_id,
                    PLATFORM,
                    store_id,
                    chat_id,
                    str(first["room_id"] or ""),
                    str(first["customer_id"] or ""),
                    str(first["customer_openid"] or ""),
                    str(first["platform_nickname"] or ""),
                    str(first["session_key"] or ""),
                    _to_int(first["sync_id"]),
                    _to_int(last["sync_id"]),
                    str(first["message_id"] or ""),
                    str(last["message_id"] or ""),
                    len(event_rows),
                    request_text,
                    lock_token,
                    now_ts_ms,
                    now_ts_ms,
                ),
            )
            turn_id = int(turn_cur.lastrowid)

            placeholders = ",".join("?" for _ in event_ids)
            updated = conn.execute(
                f"""
                UPDATE {self.TBL_INBOUND}
                SET status='processing',
                    attempts=attempts+1,
                    turn_id=?,
                    lock_token=?,
                    locked_at_ms=?,
                    updated_at_ms=?
                WHERE id IN ({placeholders})
                  AND tenant_id=? AND platform=? AND store_id=?
                  AND status='new'
                """,
                (
                    turn_id,
                    lock_token,
                    now_ts_ms,
                    now_ts_ms,
                    *event_ids,
                    tenant_id,
                    PLATFORM,
                    store_id,
                ),
            ).rowcount

            if updated != len(event_ids):
                conn.rollback()
                return None

            conn.commit()

        events = [
            TurnEvent(
                id=int(row["id"]),
                sync_id=_to_int(row["sync_id"]),
                message_id=str(row["message_id"] or ""),
                message_type=str(row["message_type"] or "text"),
                text=str(row["text"] or ""),
                media_url=str(row["media_url"] or ""),
                raw_json=str(row["raw_json"] or ""),
                event_ts_ms=_to_int(row["event_ts_ms"]),
            )
            for row in event_rows
        ]

        return PendingTurn(
            turn_id=turn_id,
            lock_token=lock_token,
            chat_id=chat_id,
            room_id=str(first["room_id"] or ""),
            customer_id=str(first["customer_id"] or ""),
            customer_openid=str(first["customer_openid"] or ""),
            platform_nickname=str(first["platform_nickname"] or ""),
            session_key=str(first["session_key"] or ""),
            first_sync_id=_to_int(first["sync_id"]),
            last_sync_id=_to_int(last["sync_id"]),
            events=events,
        )

    def mark_turn_outcome(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        room_id: str,
        customer_openid: str,
        platform_nickname: str,
        turn_id: int,
        lock_token: str,
        event_ids: List[int],
        last_sync_id: int,
        status: str,
        decision_action: str,
        decision_reason: str,
        reply_text: str,
        last_error: str = "",
    ) -> None:
        if not event_ids:
            return

        now_ts = now_ms()
        event_status = "done" if status == "replied" else "dropped"
        placeholders = ",".join("?" for _ in event_ids)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            conn.execute(
                f"""
                UPDATE {self.TBL_INBOUND}
                SET status=?,
                    lock_token='',
                    locked_at_ms=0,
                    decision_action=?,
                    decision_reason=?,
                    reply_text=?,
                    last_error=?,
                    updated_at_ms=?
                WHERE id IN ({placeholders})
                  AND status='processing'
                  AND lock_token=?
                """,
                (
                    event_status,
                    (decision_action or "")[:32],
                    (decision_reason or "")[:220],
                    (reply_text or "")[:3000],
                    (last_error or "")[:300],
                    now_ts,
                    *event_ids,
                    lock_token,
                ),
            )

            conn.execute(
                f"""
                UPDATE {self.TBL_TURN}
                SET status=?,
                    lock_token='',
                    decision_action=?,
                    decision_reason=?,
                    reply_text=?,
                    last_error=?,
                    updated_at_ms=?
                WHERE id=? AND lock_token=?
                """,
                (
                    status,
                    (decision_action or "")[:32],
                    (decision_reason or "")[:220],
                    (reply_text or "")[:3000],
                    (last_error or "")[:300],
                    now_ts,
                    turn_id,
                    lock_token,
                ),
            )

            self._upsert_chat_cursor_conn(
                conn,
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=chat_id,
                room_id=room_id,
                customer_openid=customer_openid,
                platform_nickname=platform_nickname,
                seen_sync_id=-1,
                replied_sync_id=int(last_sync_id),
                at_ms=now_ts,
            )

            # A session's first-reply timer should stop once one queued turn has been completed.
            conn.execute(
                f"""
                UPDATE {self.TBL_CHAT}
                SET sla_window_start_ms=0,
                    sla_placeholder_sent_ms=0,
                    updated_at_ms=?
                WHERE tenant_id=? AND platform=? AND store_id=? AND chat_id=?
                """,
                (now_ts, tenant_id, PLATFORM, store_id, chat_id),
            )

            conn.commit()

API_POST_SCRIPT = r"""
async (args) => {
    const cookiePairs = (document.cookie || '')
      .split(';')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((kv) => {
        const idx = kv.indexOf('=');
        if (idx < 0) return [kv, ''];
        return [kv.slice(0, idx), kv.slice(idx + 1)];
      });
    const cookies = Object.fromEntries(cookiePairs);

    const headers = {
      'content-type': 'application/json;charset=UTF-8',
      'accept': 'application/json, text/plain, */*',
      'web_version': String(
        (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.webVersion) ||
        (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.version) ||
        '2.18.5'
      ),
    };
    if (cookies.biz_magic) {
      headers['biz_magic'] = cookies.biz_magic;
    }

    const query = [];
    if (args.action) {
      query.push(`action=${encodeURIComponent(args.action)}`);
    }
    if (args.params && typeof args.params === 'object') {
      for (const [k, v] of Object.entries(args.params)) {
        if (v === null || v === undefined || v === '') continue;
        query.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
      }
    }

    const path = String(args.path || '/').startsWith('/') ? String(args.path) : `/${String(args.path || '')}`;
    let url = `/shop/commkf${path}`;
    if (query.length > 0) {
      url += `?${query.join('&')}`;
    }

    try {
      const resp = await fetch(url, {
        method: 'POST',
        credentials: 'include',
        headers,
        body: JSON.stringify(args.data || {}),
      });
      const text = await resp.text();
      return {
        ok: resp.ok,
        status: resp.status,
        url,
        text,
      };
    } catch (err) {
      return {
        ok: false,
        status: 0,
        url,
        text: '',
        error: String(err && err.message ? err.message : err),
      };
    }
}
"""


class CommKfApiClient:
    async def post(
        self,
        page: Page,
        *,
        path: str,
        action: str,
        data: Dict[str, Any],
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        out = await page.evaluate(
            API_POST_SCRIPT,
            {
                "path": path,
                "action": action,
                "data": data,
                "params": params or {},
            },
        )

        status = _to_int((out or {}).get("status"), 0)
        text = str((out or {}).get("text") or "")
        err = str((out or {}).get("error") or "").strip()
        if err:
            raise RuntimeError(f"api_request_error:{err}")

        if not text:
            raise RuntimeError(f"api_empty_response:{path}:{action}:status={status}")

        try:
            data_obj = json.loads(text)
        except Exception:
            raise RuntimeError(
                f"api_non_json:{path}:{action}:status={status}:body={text[:180]}"
            )

        if not isinstance(data_obj, dict):
            raise RuntimeError(f"api_invalid_json_type:{path}:{action}")

        if status < 200 or status >= 300:
            raise RuntimeError(
                f"api_http_status:{path}:{action}:status={status}:body={text[:180]}"
            )

        return data_obj

    async def get_sync_msg(self, page: Page, *, start_sync_id: int, num: int) -> Dict[str, Any]:
        return await self.post(
            page,
            path="/msg",
            action="get_sync_msg",
            data={
                "start_sync_id": str(max(0, int(start_sync_id))),
                "num": max(1, int(num)),
                "include_sys_msg": True,
            },
        )

    async def get_session_summary(self, page: Page) -> Dict[str, Any]:
        return await self.post(
            page,
            path="/summary",
            action="get_session_summary",
            data={},
        )

    async def get_room_msg(
        self,
        page: Page,
        *,
        room_id: str,
        limit: int = 80,
    ) -> Dict[str, Any]:
        return await self.post(
            page,
            path="/msg",
            action="get_room_msg",
            data={
                "room_id": str(room_id or "").strip(),
                "msg_id": "18446744073709551616",
                "op": 0,
                "limit": max(20, min(200, int(limit))),
            },
        )

    async def get_msg_id(self, page: Page, *, msg_id_num: int = 1) -> Dict[str, Any]:
        return await self.post(
            page,
            path="/msg",
            action="get_msg_id",
            data={"msg_id_num": max(1, int(msg_id_num))},
        )

    async def send_room_msg(
        self,
        page: Page,
        *,
        room_id: str,
        msg_id: str,
        msg_content: str,
        msg_type: int = 1,
    ) -> Dict[str, Any]:
        # Text message payload format follows official web client:
        # msg_content must be a JSON string like {"content":"..."}.
        content_json = json.dumps({"content": str(msg_content or "")}, ensure_ascii=False)
        client_ms = now_ms()
        return await self.post(
            page,
            path="/msg",
            action="send_room_msg",
            data={
                "room_id": str(room_id or "").strip(),
                "msg_id": str(msg_id or "").strip(),
                "msg_type": int(msg_type),
                "msg_content": content_json,
                "create_time_ms": client_ms,
                "client_ms": client_ms,
                "send_source": 2,
                "send_tools": 0,
                "async_check_spam_stage": 1,
            },
        )

def _api_ret(resp: Dict[str, Any]) -> int:
    base = resp.get("base_resp")
    if isinstance(base, dict):
        return _to_int(base.get("ret"), -1)
    return _to_int(resp.get("ret"), -1)


def _api_err_msg(resp: Dict[str, Any]) -> str:
    base = resp.get("base_resp")
    if isinstance(base, dict):
        msg = str(base.get("err_msg") or "").strip()
        if msg:
            return msg
    return str(resp.get("err_msg") or "").strip()


async def _pick_target_page(browser: Browser) -> Optional[Page]:
    best_page: Optional[Page] = None
    best_score = -1
    for context in browser.contexts:
        for page in context.pages:
            score = _score_page_url(page.url or "")
            if score > best_score:
                best_page = page
                best_score = score
    return best_page if best_score > 0 else None


async def _open_kf_page(browser: Browser, port: int) -> Optional[Page]:
    if not browser.contexts:
        return None
    page = await browser.contexts[0].new_page()
    try:
        await page.goto(WEIXIN_KF_URL, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(800)
    except Exception as exc:
        _log(f"[WARN] 端口 {port} 新开视频号客服页失败: {type(exc).__name__}: {exc}")
    if _score_page_url(page.url or "") > 0:
        _log(f"[INFO] 端口 {port} 已新开视频号客服页: {page.url}")
        return page
    try:
        await page.close()
    except Exception:
        pass
    return None


async def _collect_browsers(
    playwright: Playwright,
    ports: Iterable[int],
) -> List[Tuple[Browser, Page]]:
    pairs: List[Tuple[Browser, Page]] = []
    for port in ports:
        endpoint = f"http://127.0.0.1:{port}"
        _log(f"[INFO] 尝试连接 CDP 端口 {port} ({endpoint}) ...")
        try:
            browser = await playwright.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            _log(f"[WARN] 端口 {port} 连接失败: {type(exc).__name__}: {exc}")
            continue

        page = await _pick_target_page(browser)
        if page is None:
            urls: List[str] = []
            for context in browser.contexts:
                for p in context.pages:
                    u = (p.url or "").strip()
                    if u:
                        urls.append(u)
            if urls:
                _log(f"[WARN] 端口 {port} 已连接，但未找到视频号客服页。当前标签页: {' | '.join(urls[:5])}")
            else:
                _log(f"[WARN] 端口 {port} 已连接，但浏览器无可用标签页。")
            page = await _open_kf_page(browser, port)
            if page is None:
                _disconnect_browser(browser)
                continue

        _log(f"[INFO] 端口 {port} 绑定页面: {page.url}")
        pairs.append((browser, page))
    return pairs


async def _send_text_reply_via_api(
    page: Page,
    api_client: CommKfApiClient,
    *,
    room_id: str,
    reply_text: str,
    api_lock: Optional[asyncio.Lock] = None,
) -> Tuple[bool, str, str]:
    target_room_id = str(room_id or "").strip()
    text = str(reply_text or "")
    if not target_room_id:
        return False, "send_room_id_empty", ""
    if not text.strip():
        return False, "send_reply_text_empty", ""

    async def _send_once() -> Tuple[bool, str, str]:
        alloc = await api_client.get_msg_id(page, msg_id_num=1)
        alloc_ret = _api_ret(alloc)
        if alloc_ret != 0:
            err = _api_err_msg(alloc)
            return False, f"get_msg_id_ret_{alloc_ret}:{err}", ""

        msg_ids = [str(x or "").strip() for x in list(alloc.get("msg_id_list") or []) if str(x or "").strip()]
        if not msg_ids:
            return False, "get_msg_id_empty", ""
        msg_id = msg_ids[0]

        send_resp = await api_client.send_room_msg(
            page,
            room_id=target_room_id,
            msg_id=msg_id,
            msg_type=1,
            msg_content=text,
        )
        send_ret = _api_ret(send_resp)
        if send_ret != 0:
            err = _api_err_msg(send_resp)
            return False, f"send_room_msg_ret_{send_ret}:{err}", msg_id
        return True, "", msg_id

    if api_lock is None:
        return await _send_once()
    async with api_lock:
        return await _send_once()

def _normalize_gateway_text(value: str, *, message_type: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Metadata blobs from media payloads should not be sent as user text.
    if message_type in {"image", "video", "voice"} and text[:1] in {"{", "["}:
        return ""
    return text


def _extract_turn_event_media_urls(event: TurnEvent) -> List[str]:
    urls: List[str] = []

    def _push(raw: Any) -> None:
        value = str(raw or "").strip()
        if value and value not in urls:
            urls.append(value)

    _push(event.media_url)

    raw_event = _parse_json_dict(event.raw_json)
    msg_content_obj = _parse_json_dict(raw_event.get("msg_kf_content"))
    for candidate in _extract_media_urls(msg_content_obj):
        _push(candidate)

    text_blob = str(event.text or "").strip()
    if text_blob:
        _push(_extract_media_url_from_text_blob(text_blob))

    return urls


def _build_turn_gateway_event(
    turn: PendingTurn,
) -> Tuple[str, str, str, List[str], List[str]]:
    text_lines: List[str] = []
    media_urls: List[str] = []
    message_types: List[str] = []

    has_image = False
    has_video = False
    has_voice = False

    for event in turn.events:
        msg_type = str(event.message_type or "text").strip() or "text"
        message_types.append(msg_type)
        if msg_type == "image":
            has_image = True
        elif msg_type == "video":
            has_video = True
        elif msg_type == "voice":
            has_voice = True

        clean_text = _normalize_gateway_text(event.text, message_type=msg_type)
        if clean_text:
            text_lines.append(clean_text)

        for media_url in _extract_turn_event_media_urls(event):
            if media_url and media_url not in media_urls:
                media_urls.append(media_url)

    if has_image:
        message_type = "image"
    elif has_video:
        message_type = "video"
    elif has_voice:
        message_type = "voice"
    else:
        message_type = "text"

    text = "\n".join(text_lines).strip()
    if message_type == "text" and not text:
        text = "[空消息]"

    primary_media_url = media_urls[0] if media_urls else ""
    return message_type, text, primary_media_url, media_urls, message_types


def _safe_media_suffix_from_ref(media_ref: str, content_type: str = "") -> str:
    suffix = Path(urllib_parse.urlparse(str(media_ref or "")).path or "").suffix.lower()
    if not suffix and content_type:
        suffix = (mimetypes.guess_extension(content_type.split(";")[0].strip().lower()) or "").lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}:
        return suffix
    return ".jpg"


async def _materialize_media_url_to_local(
    page: Page,
    media_url: str,
    *,
    media_cache_dir: Path,
    max_bytes: int = DEFAULT_MEDIA_MAX_BYTES,
) -> str:
    source = str(media_url or "").strip()
    if not source:
        return ""
    if source.startswith("/") and Path(source).is_file():
        return source
    if source.startswith("file://"):
        local = Path(source[7:])
        return str(local) if local.is_file() else ""
    if not source.startswith(("http://", "https://")):
        return ""

    media_cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        resp = await page.context.request.get(source, timeout=15000, fail_on_status_code=False)
    except Exception as exc:
        _log(f"[WARN] 图片下载失败（request 异常）: {type(exc).__name__}: {source[:160]}")
        return ""

    if not resp.ok:
        _log(f"[WARN] 图片下载失败（HTTP {resp.status}）: {source[:160]}")
        return ""

    try:
        body = await resp.body()
    except Exception as exc:
        _log(f"[WARN] 图片下载失败（读取响应）: {type(exc).__name__}: {source[:160]}")
        return ""

    if not body:
        _log(f"[WARN] 图片下载失败（空内容）: {source[:160]}")
        return ""
    if len(body) > max_bytes:
        _log(f"[WARN] 图片下载失败（超限 {len(body)} bytes）: {source[:160]}")
        return ""

    suffix = _safe_media_suffix_from_ref(source, str(resp.headers.get("content-type") or ""))
    digest = hashlib.sha1(body).hexdigest()
    final_path = media_cache_dir / f"{digest}{suffix}"
    if not final_path.exists():
        final_path.write_bytes(body)
    return str(final_path.resolve())


async def _materialize_turn_media_urls(
    page: Page,
    media_urls: List[str],
    *,
    media_cache_dir: Path,
) -> Tuple[str, List[str]]:
    local_urls: List[str] = []
    for raw in media_urls:
        source = str(raw or "").strip()
        if not source:
            continue
        local = await _materialize_media_url_to_local(
            page,
            source,
            media_cache_dir=media_cache_dir,
        )
        if not local:
            _log(f"[WARN] 图片本地化失败，跳过该图片: {source[:160]}")
            continue
        if local not in local_urls:
            local_urls.append(local)
    primary = local_urls[0] if local_urls else ""
    return primary, local_urls


async def _refresh_session_cache(
    page: Page,
    api_client: CommKfApiClient,
    event_store: WeixinEventStore,
    *,
    tenant_id: str,
    store_id: str,
    session_cache: Dict[str, Dict[str, str]],
    api_lock: Optional[asyncio.Lock] = None,
) -> Tuple[int, List[str]]:
    if api_lock is None:
        resp = await api_client.get_session_summary(page)
    else:
        async with api_lock:
            resp = await api_client.get_session_summary(page)
    ret = _api_ret(resp)
    if ret != 0:
        raise RuntimeError(f"get_session_summary_ret_{ret}")

    summary_list = list(resp.get("summary_list") or [])
    pin_list = list(resp.get("pin_summary_list") or [])
    merged = pin_list + summary_list

    refreshed = 0
    room_ids: List[str] = []
    for item in merged:
        room_id = str(item.get("room_id") or "").strip()
        if not room_id:
            continue
        extra_info = _parse_json_dict(item.get("extra_info"))
        nickname = _normalize_nickname(str(extra_info.get("nickname") or ""))
        customer_openid = str(item.get("user_openid") or item.get("send_openid") or "").strip()
        summary_sync_id = _to_int(item.get("sync_id"), 0)

        session_cache[room_id] = {
            "nickname": nickname,
            "customer_openid": customer_openid,
            "summary_sync_id": str(summary_sync_id),
        }
        event_store.upsert_chat_cursor(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=room_id,
            room_id=room_id,
            customer_openid=customer_openid,
            platform_nickname=nickname,
            seen_sync_id=-1,
            replied_sync_id=-1,
        )
        refreshed += 1
        room_ids.append(room_id)

    visible = set(room_ids)
    for chat_id in list(session_cache.keys()):
        if chat_id not in visible:
            session_cache.pop(chat_id, None)

    event_store.prune_chat_baseline_not_in(
        tenant_id=tenant_id,
        store_id=store_id,
        keep_chat_ids=room_ids,
    )

    return refreshed, room_ids


def _events_after_baseline(
    events: List[Dict[str, Any]],
    *,
    baseline_message_id: str,
    baseline_event_ts_ms: int,
) -> List[Dict[str, Any]]:
    if not events:
        return []
    bid = str(baseline_message_id or "").strip()
    if not bid:
        return []

    anchor_idx = -1
    for idx, event_payload in enumerate(events):
        if str(event_payload.get("message_id") or "").strip() == bid:
            anchor_idx = idx
    if anchor_idx >= 0:
        return events[anchor_idx + 1 :]

    out: List[Dict[str, Any]] = []
    baseline_ts = max(0, int(baseline_event_ts_ms))
    for event_payload in events:
        evt_ts = _to_int(event_payload.get("event_ts_ms"), 0)
        evt_mid = str(event_payload.get("message_id") or "").strip()
        if evt_ts > baseline_ts or (evt_ts == baseline_ts and evt_mid > bid):
            out.append(event_payload)
    return out


async def _ingest_room_recent_messages(
    page: Page,
    api_client: CommKfApiClient,
    event_store: WeixinEventStore,
    *,
    tenant_id: str,
    store_id: str,
    room_id: str,
    room_seq: int,
    room_msg_limit: int,
    session_cache: Dict[str, Dict[str, str]],
    api_lock: Optional[asyncio.Lock] = None,
) -> int:
    rid = str(room_id or "").strip()
    if not rid:
        return 0

    try:
        if api_lock is None:
            resp = await api_client.get_room_msg(page, room_id=rid, limit=room_msg_limit)
        else:
            async with api_lock:
                resp = await api_client.get_room_msg(page, room_id=rid, limit=room_msg_limit)
    except Exception as exc:
        _log(f"[WARN] room 拉取失败 room_id={rid}, exc={type(exc).__name__}:{exc}")
        return 0

    ret = _api_ret(resp)
    if ret != 0:
        _log(f"[WARN] room 拉取失败 room_id={rid}, ret={ret}, msg={_api_err_msg(resp)}")
        return 0

    rows = list(resp.get("list") or resp.get("msg_list") or [])
    if not rows:
        return 0

    cached = session_cache.get(rid) or {}
    fallback_nickname = str(cached.get("nickname") or "").strip()
    events: List[Dict[str, Any]] = []
    for raw in rows:
        raw_room_id = str(raw.get("room_id") or "").strip()
        if raw_room_id and raw_room_id != rid:
            continue

        event_payload = _normalize_customer_inbound_event(
            raw,
            kf_openid="",
            store_id=store_id,
            fallback_nickname=fallback_nickname,
        )
        if event_payload is None:
            continue

        events.append(event_payload)

    if not events:
        return 0

    events.sort(
        key=lambda item: (
            _to_int(item.get("event_ts_ms"), 0),
            str(item.get("message_id") or ""),
        )
    )
    baseline_message_id, baseline_event_ts_ms = event_store.get_chat_baseline(
        tenant_id=tenant_id,
        store_id=store_id,
        chat_id=rid,
    )
    baseline_missing = not baseline_message_id
    if baseline_missing:
        events_to_ingest = [events[-1]]
    else:
        events_to_ingest = _events_after_baseline(
            events,
            baseline_message_id=baseline_message_id,
            baseline_event_ts_ms=baseline_event_ts_ms,
        )
    if not events_to_ingest:
        return 0

    inserted = event_store.ingest_room_compensate_events(
        tenant_id=tenant_id,
        store_id=store_id,
        events=events_to_ingest,
        anchor_sync_id=max(1, int(room_seq)),
    )
    if baseline_missing:
        anchor = events_to_ingest[-1]
        event_store.set_chat_baseline(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=rid,
            baseline_message_id=str(anchor.get("message_id") or ""),
            baseline_event_ts_ms=_to_int(anchor.get("event_ts_ms"), 0),
            at_ms=now_ms(),
        )
        _log(
            f"[INFO] room 基线已建立 room_id={rid}, "
            f"baseline_msg_id={str(anchor.get('message_id') or '')[:24]}"
        )
    if inserted > 0:
        _log(f"[INFO] room 入队 room_id={rid}, 入队 {inserted} 条。")
    return inserted


async def _ingest_once(
    page: Page,
    api_client: CommKfApiClient,
    event_store: WeixinEventStore,
    *,
    tenant_id: str,
    store_id: str,
    session_cache: Dict[str, Dict[str, str]],
    room_msg_limit: int,
    api_lock: Optional[asyncio.Lock] = None,
) -> Tuple[int, int]:
    refreshed, room_ids = await _refresh_session_cache(
        page,
        api_client,
        event_store,
        tenant_id=tenant_id,
        store_id=store_id,
        session_cache=session_cache,
        api_lock=api_lock,
    )
    if not room_ids:
        return 0, 0

    inserted = 0
    scanned_rooms = 0
    seq_base = now_ms()

    for idx, rid in enumerate(room_ids):
        scanned_rooms += 1
        inserted += await _ingest_room_recent_messages(
            page,
            api_client,
            event_store,
            tenant_id=tenant_id,
            store_id=store_id,
            room_id=rid,
            room_seq=seq_base + idx,
            room_msg_limit=room_msg_limit,
            session_cache=session_cache,
            api_lock=api_lock,
        )

    return inserted, scanned_rooms


async def _process_claimed_turn(
    turn: PendingTurn,
    page: Page,
    decision_client: DecisionClient,
    api_client: CommKfApiClient,
    event_store: WeixinEventStore,
    state_store: LocalStateStore,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    first_reply_text: str,
    voice_fallback_text: str,
    media_cache_dir: Path,
    api_lock: Optional[asyncio.Lock] = None,
) -> bool:
    target_room_id = str(turn.room_id or turn.chat_id or "").strip()
    if not target_room_id:
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            room_id=turn.chat_id,
            customer_openid=turn.customer_openid,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            last_sync_id=turn.last_sync_id,
            status="dropped",
            decision_action="drop",
            decision_reason="room_id_empty",
            reply_text="",
            last_error="room_id_empty",
        )
        _log(f"[WARN][{turn.session_key}] 缺少 room_id，回合已丢弃。")
        return True

    gateway_msg_type, turn_text, turn_media_url, turn_media_urls, turn_message_types = _build_turn_gateway_event(turn)
    turn_media_urls_for_gateway = list(turn_media_urls)
    incoming_raw = {
        "page_url": page.url,
        "channel_capabilities": dict(CHANNEL_CAPABILITIES),
        "chat_id": turn.chat_id,
        "room_id": target_room_id,
        "turn_id": turn.turn_id,
        "turn_event_ids": turn.event_ids,
        "turn_message_ids": [e.message_id for e in turn.events],
        "turn_message_types": turn_message_types,
        "turn_first_sync_id": turn.first_sync_id,
        "turn_last_sync_id": turn.last_sync_id,
        "turn_event_count": len(turn.events),
        "media_urls": turn_media_urls_for_gateway,
        "media_urls_raw": turn_media_urls,
    }

    if gateway_msg_type == "voice":
        voice_reply_text = str(voice_fallback_text or "").strip()
        voice_incoming = IncomingMessage(
            tenant_id=tenant_id,
            platform=PLATFORM,
            store_id=store_id,
            store_name=store_name or store_id,
            customer_id=turn.customer_id,
            platform_nickname=turn.platform_nickname or f"room_{turn.chat_id[-6:]}",
            message_id=turn.events[-1].message_id,
            message_type="voice",
            text=turn_text or "[语音]",
            media_url=turn_media_url,
            raw=dict(incoming_raw),
            role="customer",
        )

        _log(
            f"[INFO][{voice_incoming.session_key()}] 出队回合 turn_id={turn.turn_id}, "
            f"events={len(turn.events)}, sync={turn.first_sync_id}->{turn.last_sync_id}, "
            f"type=voice, media={len(turn_media_urls_for_gateway)}"
        )

        if not voice_reply_text:
            event_store.mark_turn_outcome(
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=turn.chat_id,
                room_id=target_room_id,
                customer_openid=turn.customer_openid,
                platform_nickname=turn.platform_nickname,
                turn_id=turn.turn_id,
                lock_token=turn.lock_token,
                event_ids=turn.event_ids,
                last_sync_id=turn.last_sync_id,
                status="dropped",
                decision_action="drop",
                decision_reason="voice_fallback_text_empty",
                reply_text="",
                last_error="voice_fallback_text_empty",
            )
            _log(f"[WARN][{voice_incoming.session_key()}] 语音兜底回复为空，回合已丢弃。")
            return True

        try:
            sent_ok, send_error, sent_msg_id = await _send_text_reply_via_api(
                page,
                api_client,
                room_id=target_room_id,
                reply_text=voice_reply_text,
                api_lock=api_lock,
            )
        except Exception as exc:
            sent_ok = False
            send_error = f"send_api_exception:{type(exc).__name__}"
            sent_msg_id = ""

        if not sent_ok:
            event_store.mark_turn_outcome(
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=turn.chat_id,
                room_id=target_room_id,
                customer_openid=turn.customer_openid,
                platform_nickname=turn.platform_nickname,
                turn_id=turn.turn_id,
                lock_token=turn.lock_token,
                event_ids=turn.event_ids,
                last_sync_id=turn.last_sync_id,
                status="dropped",
                decision_action="send",
                decision_reason="voice_auto_reply:send_failed",
                reply_text="",
                last_error=(send_error or "send_failed"),
            )
            _log(
                f"[WARN][{voice_incoming.session_key()}] 语音固定回复发送失败，已丢弃。"
                f"reason={send_error or 'send_failed'}"
            )
            return True

        state_store.mark_reply(voice_incoming, now_ms(), reply_text=voice_reply_text)
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            room_id=target_room_id,
            customer_openid=turn.customer_openid,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            last_sync_id=turn.last_sync_id,
            status="replied",
            decision_action="send",
            decision_reason="voice_auto_reply",
            reply_text=voice_reply_text,
        )
        _log(f"[INFO][{voice_incoming.session_key()}] 语音固定回复已发送。msg_id={sent_msg_id}")
        return True

    should_localize_media = (
        gateway_msg_type == "image"
        or (ORDER_CARD_MSG_TYPE in turn_message_types and bool(turn_media_urls))
    )
    if should_localize_media:
        if not turn_media_urls:
            if gateway_msg_type == "image":
                event_store.mark_turn_outcome(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    chat_id=turn.chat_id,
                    room_id=target_room_id,
                    customer_openid=turn.customer_openid,
                    platform_nickname=turn.platform_nickname,
                    turn_id=turn.turn_id,
                    lock_token=turn.lock_token,
                    event_ids=turn.event_ids,
                    last_sync_id=turn.last_sync_id,
                    status="dropped",
                    decision_action="drop",
                    decision_reason="image_media_url_empty",
                    reply_text="",
                    last_error="image_media_url_empty",
                )
                _log(f"[WARN][{turn.session_key}] 图片回合无可用链接，已丢弃。")
                return True
            _log(f"[WARN][{turn.session_key}] 订单卡片无可用图片链接，继续按文本处理。")
        local_primary, local_urls = await _materialize_turn_media_urls(
            page,
            turn_media_urls,
            media_cache_dir=media_cache_dir,
        )
        if not local_urls:
            if gateway_msg_type == "image":
                event_store.mark_turn_outcome(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    chat_id=turn.chat_id,
                    room_id=target_room_id,
                    customer_openid=turn.customer_openid,
                    platform_nickname=turn.platform_nickname,
                    turn_id=turn.turn_id,
                    lock_token=turn.lock_token,
                    event_ids=turn.event_ids,
                    last_sync_id=turn.last_sync_id,
                    status="dropped",
                    decision_action="drop",
                    decision_reason="image_media_localize_failed",
                    reply_text="",
                    last_error="image_media_localize_failed",
                )
                _log(f"[WARN][{turn.session_key}] 图片本地化全部失败，已丢弃该回合。")
                return True
            _log(f"[WARN][{turn.session_key}] 订单卡片图片本地化失败，回退使用原始链接。")
        else:
            turn_media_urls_for_gateway = local_urls
            turn_media_url = local_primary
            local_cnt = sum(1 for item in local_urls if item.startswith("/"))
            _log(
                f"[INFO][{turn.session_key}] 图片本地化: {local_cnt}/{len(turn_media_urls)} "
                f"primary={turn_media_url[:120]}"
            )

    incoming = IncomingMessage(
        tenant_id=tenant_id,
        platform=PLATFORM,
        store_id=store_id,
        store_name=store_name or store_id,
        customer_id=turn.customer_id,
        platform_nickname=turn.platform_nickname or f"room_{turn.chat_id[-6:]}",
        message_id=turn.events[-1].message_id,
        message_type=gateway_msg_type,
        text=turn_text,
        media_url=turn_media_url,
        raw={
            **incoming_raw,
            "media_urls": turn_media_urls_for_gateway,
        },
        role="customer",
    )

    _log(
        f"[INFO][{incoming.session_key()}] 出队回合 turn_id={turn.turn_id}, "
        f"events={len(turn.events)}, sync={turn.first_sync_id}->{turn.last_sync_id}, "
        f"type={incoming.message_type}, media={len(turn_media_urls_for_gateway)}"
    )

    decision = None
    placeholder_sent = False
    placeholder_msg_id = ""
    placeholder_error = ""
    sla_start_ms, _ = event_store.get_chat_sla_state(
        tenant_id=tenant_id,
        store_id=store_id,
        chat_id=turn.chat_id,
    )
    timer_active = sla_start_ms > 0
    first_reply_deadline_ms = (
        sla_start_ms + int(DEFAULT_FIRST_REPLY_DEADLINE_MS)
        if timer_active
        else 0
    )
    try:
        decide_task = asyncio.create_task(decision_client.decide(incoming))
        try:
            if not timer_active:
                decision = await decide_task
            else:
                remain_ms = first_reply_deadline_ms - now_ms()
                if remain_ms > 0:
                    decision = await asyncio.wait_for(
                        asyncio.shield(decide_task),
                        timeout=max(0.05, remain_ms / 1000.0),
                    )
                else:
                    raise asyncio.TimeoutError()
        except asyncio.TimeoutError:
            text = str(first_reply_text or "").strip()
            if text:
                try:
                    placeholder_sent, placeholder_error, placeholder_msg_id = await _send_text_reply_via_api(
                        page,
                        api_client,
                        room_id=target_room_id,
                        reply_text=text,
                        api_lock=api_lock,
                    )
                except Exception as exc:
                    placeholder_sent = False
                    placeholder_msg_id = ""
                    placeholder_error = f"send_api_exception:{type(exc).__name__}"
            else:
                placeholder_sent = False
                placeholder_msg_id = ""
                placeholder_error = "fallback_text_empty"

            if timer_active:
                event_store.mark_chat_sla_placeholder_sent(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    chat_id=turn.chat_id,
                    at_ms=now_ms(),
                )
                if placeholder_sent:
                    state_store.mark_reply(incoming, now_ms(), reply_text=text)
                    _log(f"[INFO][{incoming.session_key()}] 8秒首响兜底已发送。")
                else:
                    _log(
                        f"[WARN][{incoming.session_key()}] 8秒首响兜底发送失败。"
                        f"reason={placeholder_error or 'send_failed'}"
                    )
            decision = await decide_task
    except Exception as exc:
        _log(f"[WARN][{incoming.session_key()}] 决策请求异常: {type(exc).__name__}: {exc}")
        decision = None

    action = str((decision.action if decision else "skip") or "").strip().lower()
    reason = str((decision.reason if decision else "request_failed:unknown") or "").strip()
    _log(f"[INFO][{incoming.session_key()}] decision action={action}, reason={reason}")

    decision_reply_text = (decision.reply_text if decision else "") or ""
    decision_reply_text = decision_reply_text.strip()
    delivered_text = str(first_reply_text or "").strip() if placeholder_sent else ""
    delivered_msg_id = placeholder_msg_id if placeholder_sent else ""
    send_error = placeholder_error if placeholder_error else ""
    decision_reason = reason

    should_send_final = action == "send" and bool(decision_reply_text)
    if should_send_final and (not placeholder_sent or decision_reply_text != delivered_text):
        try:
            sent_ok, send_error, sent_msg_id = await _send_text_reply_via_api(
                page,
                api_client,
                room_id=target_room_id,
                reply_text=decision_reply_text,
                api_lock=api_lock,
            )
        except Exception as exc:
            sent_ok = False
            send_error = f"send_api_exception:{type(exc).__name__}"
            sent_msg_id = ""

        if sent_ok:
            delivered_text = decision_reply_text
            delivered_msg_id = sent_msg_id
            state_store.mark_reply(incoming, now_ms(), reply_text=decision_reply_text)
            if placeholder_sent:
                decision_reason = f"{decision_reason}:first_reply_then_final".strip(":")
        elif not placeholder_sent:
            event_store.mark_turn_outcome(
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=turn.chat_id,
                room_id=target_room_id,
                customer_openid=turn.customer_openid,
                platform_nickname=turn.platform_nickname,
                turn_id=turn.turn_id,
                lock_token=turn.lock_token,
                event_ids=turn.event_ids,
                last_sync_id=turn.last_sync_id,
                status="dropped",
                decision_action="send",
                decision_reason=f"{decision_reason}:send_failed".strip(":"),
                reply_text="",
                last_error=(send_error or "send_failed"),
            )
            _log(f"[WARN][{incoming.session_key()}] 回合发送失败，已丢弃。reason={send_error or 'send_failed'}")
            return True
        else:
            decision_reason = f"{decision_reason}:final_send_failed_keep_first_reply".strip(":")
            _log(
                f"[WARN][{incoming.session_key()}] 正式回复发送失败，保留首响回复。"
                f"reason={send_error or 'send_failed'}"
            )

    if (not delivered_text) and action == "send" and (not decision_reply_text):
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            room_id=target_room_id,
            customer_openid=turn.customer_openid,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            last_sync_id=turn.last_sync_id,
            status="dropped",
            decision_action="send",
            decision_reason=f"{decision_reason}:empty_reply_text".strip(":"),
            reply_text="",
            last_error="empty_reply_text",
        )
        _log(f"[WARN][{incoming.session_key()}] 决策回复为空，回合已丢弃。")
        return True

    if not delivered_text:
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            room_id=target_room_id,
            customer_openid=turn.customer_openid,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            last_sync_id=turn.last_sync_id,
            status="dropped",
            decision_action=action or "skip",
            decision_reason=decision_reason or f"decision_{action or 'skip'}",
            reply_text="",
            last_error=(send_error or ""),
        )
        return True

    if placeholder_sent and (not should_send_final or decision_reply_text == delivered_text):
        decision_reason = f"{decision_reason}:first_reply_only".strip(":")

    event_store.mark_turn_outcome(
        tenant_id=tenant_id,
        store_id=store_id,
        chat_id=turn.chat_id,
        room_id=target_room_id,
        customer_openid=turn.customer_openid,
        platform_nickname=turn.platform_nickname,
        turn_id=turn.turn_id,
        lock_token=turn.lock_token,
        event_ids=turn.event_ids,
        last_sync_id=turn.last_sync_id,
        status="replied",
        decision_action="send" if delivered_text else (action or "skip"),
        decision_reason=decision_reason or "sent",
        reply_text=delivered_text,
        last_error="",
    )
    _log(f"[INFO][{incoming.session_key()}] 回合已发送。msg_id={delivered_msg_id}")
    return True


async def _process_claimed_turn_safe(
    turn: PendingTurn,
    page: Page,
    decision_client: DecisionClient,
    api_client: CommKfApiClient,
    event_store: WeixinEventStore,
    state_store: LocalStateStore,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    first_reply_text: str,
    voice_fallback_text: str,
    media_cache_dir: Path,
    api_lock: Optional[asyncio.Lock] = None,
) -> bool:
    try:
        return await _process_claimed_turn(
            turn,
            page,
            decision_client,
            api_client,
            event_store,
            state_store,
            tenant_id=tenant_id,
            store_id=store_id,
            store_name=store_name,
            first_reply_text=first_reply_text,
            voice_fallback_text=voice_fallback_text,
            media_cache_dir=media_cache_dir,
            api_lock=api_lock,
        )
    except Exception as exc:
        err = f"unexpected:{type(exc).__name__}"
        _log(f"[ERROR][{turn.session_key}] 回合处理异常: {type(exc).__name__}: {exc}")
        try:
            event_store.mark_turn_outcome(
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=turn.chat_id,
                room_id=str(turn.room_id or turn.chat_id or ""),
                customer_openid=turn.customer_openid,
                platform_nickname=turn.platform_nickname,
                turn_id=turn.turn_id,
                lock_token=turn.lock_token,
                event_ids=turn.event_ids,
                last_sync_id=turn.last_sync_id,
                status="dropped",
                decision_action="error",
                decision_reason="turn_process_exception",
                reply_text="",
                last_error=err,
            )
        except Exception as mark_exc:
            _log(
                f"[ERROR][{turn.session_key}] 回合异常后状态回写失败: "
                f"{type(mark_exc).__name__}: {mark_exc}"
            )
        return True


async def _ingest_loop(
    page: Page,
    api_client: CommKfApiClient,
    event_store: WeixinEventStore,
    health_reporter: WorkerHealthReporter,
    *,
    tenant_id: str,
    store_id: str,
    session_cache: Dict[str, Dict[str, str]],
    room_msg_limit: int,
    ingest_poll_ms: int,
    api_lock: asyncio.Lock,
) -> None:
    idle_sleep_sec = max(0.12, int(ingest_poll_ms) / 1000.0)
    login_probe_every_loops = max(1, int(round(3000 / max(120, int(ingest_poll_ms)))))
    loop_count = 0
    while True:
        inserted = 0
        scanned_rooms = 0
        loop_count += 1
        if loop_count == 1 or loop_count % login_probe_every_loops == 0:
            login_page_detail = await _detect_login_page_detail(page)
            if login_page_detail:
                health_reporter.manual_action(login_page_detail)
                _log(f"[WARN] 页面登录态异常: {login_page_detail}")
                await asyncio.sleep(max(1.5, idle_sleep_sec))
                continue
        try:
            inserted, scanned_rooms = await _ingest_once(
                page,
                api_client,
                event_store,
                tenant_id=tenant_id,
                store_id=store_id,
                session_cache=session_cache,
                room_msg_limit=room_msg_limit,
                api_lock=api_lock,
            )
        except Exception as exc:
            category, detail = classify_worker_exception(exc)
            if category == CATEGORY_FATAL_LOCAL_STATE:
                health_reporter.fatal(detail)
                _log(f"[ERROR] API 采集致命异常: {detail}")
                raise WorkerFatalError(detail) from exc
            if category == CATEGORY_MANUAL_ACTION:
                health_reporter.manual_action(detail)
            else:
                health_reporter.degraded(detail)
            _log(f"[WARN] API 采集异常: {type(exc).__name__}: {exc}")
        else:
            health_reporter.ready("monitoring", progress=inserted > 0)

        if inserted > 0:
            _log(f"[INFO] API 采集完成：扫描会话 {scanned_rooms} 个，入队 {inserted} 条。")

        await asyncio.sleep(0.06 if inserted > 0 else idle_sleep_sec)


async def _kf_page_guard_loop(
    page: Page,
    health_reporter: WorkerHealthReporter,
) -> None:
    while True:
        current_url = "" if page.is_closed() else str(page.url or "").strip()
        if _score_page_url(current_url) <= 0:
            detail = f"kf_page_navigated:{current_url or 'page_closed'}"
            health_reporter.degraded(detail)
            _log(f"[WARN] 视频号客服页已离开客服接待页面: {current_url or 'page_closed'}")
            raise RuntimeError(detail)
        await asyncio.sleep(2)


async def _dispatch_loop(
    page: Page,
    decision_client: DecisionClient,
    api_client: CommKfApiClient,
    event_store: WeixinEventStore,
    state_store: LocalStateStore,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    first_reply_text: str,
    voice_fallback_text: str,
    max_dispatch_per_loop: int,
    media_cache_dir: Path,
    ingest_poll_ms: int,
    api_lock: asyncio.Lock,
) -> None:
    idle_sleep_sec = max(0.12, int(ingest_poll_ms) / 1000.0)
    max_inflight = max(1, min(5, int(max_dispatch_per_loop)))
    inflight_tasks: Dict[asyncio.Task[bool], str] = {}
    inflight_chats: set[str] = set()

    try:
        while True:
            claimed = 0
            while len(inflight_tasks) < max_inflight:
                turn = event_store.claim_next_turn(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    now_ts_ms=now_ms(),
                    exclude_chat_ids=inflight_chats,
                )
                if turn is None:
                    break

                chat_id = str(turn.chat_id or "")
                task = asyncio.create_task(
                    _process_claimed_turn_safe(
                        turn,
                        page,
                        decision_client,
                        api_client,
                        event_store,
                        state_store,
                        tenant_id=tenant_id,
                        store_id=store_id,
                        store_name=store_name,
                        first_reply_text=first_reply_text,
                        voice_fallback_text=voice_fallback_text,
                        media_cache_dir=media_cache_dir,
                        api_lock=api_lock,
                    )
                )
                inflight_tasks[task] = chat_id
                if chat_id:
                    inflight_chats.add(chat_id)
                claimed += 1

            if not inflight_tasks:
                await asyncio.sleep(idle_sleep_sec)
                continue

            done, _ = await asyncio.wait(
                list(inflight_tasks.keys()),
                timeout=(0.02 if claimed > 0 else 0.2),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue

            for task in done:
                chat_id = inflight_tasks.pop(task, "")
                if chat_id:
                    inflight_chats.discard(chat_id)
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    _log(f"[ERROR] dispatch task 异常: {type(exc).__name__}: {exc}")
    finally:
        for task in list(inflight_tasks.keys()):
            if not task.done():
                task.cancel()
        if inflight_tasks:
            await asyncio.gather(*list(inflight_tasks.keys()), return_exceptions=True)


async def monitor_single_browser(
    page: Page,
    decision_client: DecisionClient,
    state_store: LocalStateStore,
    event_store: WeixinEventStore,
    api_client: CommKfApiClient,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    first_reply_text: str,
    voice_fallback_text: str,
    ingest_poll_ms: int,
    room_msg_limit: int,
    max_dispatch_per_loop: int,
    media_cache_dir: Path,
    health_reporter: WorkerHealthReporter,
) -> None:
    session_cache: Dict[str, Dict[str, str]] = {}
    api_lock = asyncio.Lock()

    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(800)
        health_reporter.ready("page_attached")
        session_cache.update(
            event_store.load_chat_cache(
                tenant_id=tenant_id,
                store_id=store_id,
            )
        )

        ingest_task = asyncio.create_task(
            _ingest_loop(
                page,
                api_client,
                event_store,
                health_reporter,
                tenant_id=tenant_id,
                store_id=store_id,
                session_cache=session_cache,
                room_msg_limit=room_msg_limit,
                ingest_poll_ms=ingest_poll_ms,
                api_lock=api_lock,
            )
        )
        dispatch_task = asyncio.create_task(
            _dispatch_loop(
                page,
                decision_client,
                api_client,
                event_store,
                state_store,
                tenant_id=tenant_id,
                store_id=store_id,
                store_name=store_name,
                first_reply_text=first_reply_text,
                voice_fallback_text=voice_fallback_text,
                max_dispatch_per_loop=max_dispatch_per_loop,
                media_cache_dir=media_cache_dir,
                ingest_poll_ms=ingest_poll_ms,
                api_lock=api_lock,
            )
        )
        page_guard_task = asyncio.create_task(
            _kf_page_guard_loop(page, health_reporter)
        )

        try:
            await asyncio.gather(ingest_task, dispatch_task, page_guard_task)
        finally:
            for task in (ingest_task, dispatch_task, page_guard_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                ingest_task,
                dispatch_task,
                page_guard_task,
                return_exceptions=True,
            )

    except WorkerFatalError:
        raise
    except Exception as exc:
        category, detail = classify_worker_exception(exc)
        if category == CATEGORY_FATAL_LOCAL_STATE:
            health_reporter.fatal(detail)
            raise WorkerFatalError(detail) from exc
        if category == CATEGORY_MANUAL_ACTION:
            health_reporter.manual_action(detail)
        else:
            health_reporter.degraded(detail)
        _log(f"[ERROR] 浏览器页面监控异常: {type(exc).__name__}: {exc}")


async def run_loop(
    ports: Iterable[int],
    decision_client: DecisionClient,
    state_store: LocalStateStore,
    event_store: WeixinEventStore,
    api_client: CommKfApiClient,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    first_reply_text: str,
    voice_fallback_text: str,
    ingest_poll_ms: int,
    room_msg_limit: int,
    max_dispatch_per_loop: int,
    media_cache_dir: Path,
    health_reporter: WorkerHealthReporter,
) -> None:
    _log(f"[INFO] Worker 启动，待连接端口: {list(ports)}")
    async with async_playwright() as playwright:
        while True:
            browser_page_pairs = await _collect_browsers(playwright, ports)
            if not browser_page_pairs:
                health_reporter.manual_action("视频号客服离线：无法打开客服接待页")
                _log("[WARN] 未找到视频号客服页，3秒后重试。")
                await asyncio.sleep(3)
                continue

            health_reporter.ready("page_bound")
            tasks: List[Tuple[asyncio.Task[Any], Browser]] = []
            try:
                for browser, page in browser_page_pairs:
                    task = asyncio.create_task(
                        monitor_single_browser(
                            page=page,
                            decision_client=decision_client,
                            state_store=state_store,
                            event_store=event_store,
                            api_client=api_client,
                            tenant_id=tenant_id,
                            store_id=store_id,
                            store_name=store_name,
                            first_reply_text=first_reply_text,
                            voice_fallback_text=voice_fallback_text,
                            ingest_poll_ms=ingest_poll_ms,
                            room_msg_limit=room_msg_limit,
                            max_dispatch_per_loop=max_dispatch_per_loop,
                            media_cache_dir=media_cache_dir,
                            health_reporter=health_reporter,
                        )
                    )
                    tasks.append((task, browser))

                await asyncio.gather(*(t for t, _ in tasks))
                health_reporter.degraded("monitor_task_exited")
                _log("[WARN] 监控任务已退出，1秒后自动重连。")
            finally:
                for task, browser in tasks:
                    if not task.done():
                        task.cancel()
                    _disconnect_browser(browser)
            await asyncio.sleep(1)


def _disconnect_browser(browser: Browser) -> None:
    connection = getattr(browser, "_connection", None)
    if connection and hasattr(connection, "close"):
        try:
            connection.close()
            return
        except Exception:
            pass
    close_coro = getattr(browser, "close", None)
    if callable(close_coro):
        try:
            result = close_coro()
            if asyncio.iscoroutine(result):
                try:
                    asyncio.create_task(result)
                except RuntimeError:
                    pass
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="视频号客服 Worker（v2 API 事件队列架构）")
    parser.add_argument(
        "--ports",
        type=int,
        nargs="+",
        default=list(DEFAULT_PORTS),
        help="Chrome 远程调试端口，默认 9224",
    )
    parser.add_argument("--tenant-id", default="default", help="租户 ID")
    parser.add_argument("--store-id", default=DEFAULT_STORE_ID, help="店铺 ID")
    parser.add_argument("--store-name", default=DEFAULT_STORE_NAME, help="店铺展示名")
    parser.add_argument(
        "--decision-url",
        default=DEFAULT_DECISION_URL,
        help="Ubuntu 决策服务地址，不含路径",
    )
    parser.add_argument("--decision-key", default=DEFAULT_DECISION_KEY, help="决策服务 API Key")
    parser.add_argument(
        "--fallback-text",
        default=DEFAULT_FALLBACK_REPLY,
        help="8秒首响兜底回复文本",
    )
    parser.add_argument(
        "--voice-fallback-text",
        default=DEFAULT_VOICE_FALLBACK_REPLY,
        help="语音消息固定回复文本",
    )
    parser.add_argument(
        "--state-db",
        default=DEFAULT_STATE_DB,
        help="本地 SQLite 状态文件",
    )
    parser.add_argument(
        "--decision-timeout-sec",
        type=float,
        default=DEFAULT_DECISION_TIMEOUT_SEC,
        help="决策服务超时时间（秒）",
    )
    parser.add_argument(
        "--min-reply-interval-sec",
        type=float,
        default=0.0,
        help="兼容参数：当前版本未使用",
    )
    parser.add_argument(
        "--ingest-poll-ms",
        type=int,
        default=DEFAULT_INGEST_POLL_MS,
        help="无任务时 ingest 轮询间隔（毫秒）",
    )
    parser.add_argument(
        "--room-msg-limit",
        type=int,
        default=DEFAULT_ROOM_MSG_LIMIT,
        help="每个会话单次 get_room_msg 拉取条数",
    )
    parser.add_argument(
        "--max-dispatch-per-loop",
        type=int,
        default=DEFAULT_MAX_DISPATCH_PER_LOOP,
        help="处理并发上限（每会话单飞，最大 5）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _log(
        "[INFO] 启动参数: "
        f"ports={args.ports}, store_id={args.store_id}, store_name={args.store_name or args.store_id}, "
        f"decision_url={args.decision_url}, state_db={args.state_db}, timeout_sec={args.decision_timeout_sec}, "
        f"ingest_poll_ms={args.ingest_poll_ms}, room_msg_limit={args.room_msg_limit}, "
        f"max_dispatch_per_loop={args.max_dispatch_per_loop}"
    )

    fallback_text = str(args.fallback_text or DEFAULT_FALLBACK_REPLY).strip() or DEFAULT_FALLBACK_REPLY
    decision_client = DecisionClient(
        base_url=args.decision_url,
        api_key=args.decision_key,
        timeout_sec=args.decision_timeout_sec,
        fallback_text=fallback_text,
    )
    first_reply_text = fallback_text
    voice_fallback_text = (
        str(args.voice_fallback_text or DEFAULT_VOICE_FALLBACK_REPLY).strip()
        or DEFAULT_VOICE_FALLBACK_REPLY
    )
    state_store = LocalStateStore(args.state_db)
    event_store = WeixinEventStore(args.state_db)
    api_client = CommKfApiClient()
    health_reporter = WorkerHealthReporter.from_env(
        worker_key=f"{PLATFORM}_{args.store_id}",
        worker_kind=PLATFORM,
        store_id=args.store_id,
        store_name=args.store_name or args.store_id,
    )
    media_cache_dir = Path(args.state_db).resolve().parent / "media" / "weixin" / args.store_id
    media_cache_dir.mkdir(parents=True, exist_ok=True)
    _log(f"[INFO] 图片保存目录: {media_cache_dir}")

    health_reporter.start("booting")
    try:
        asyncio.run(
            run_loop(
                ports=args.ports,
                decision_client=decision_client,
                state_store=state_store,
                event_store=event_store,
                api_client=api_client,
                tenant_id=args.tenant_id,
                store_id=args.store_id,
                store_name=args.store_name,
                first_reply_text=first_reply_text,
                voice_fallback_text=voice_fallback_text,
                ingest_poll_ms=max(80, int(args.ingest_poll_ms)),
                room_msg_limit=max(20, min(200, int(args.room_msg_limit))),
                max_dispatch_per_loop=max(1, int(args.max_dispatch_per_loop)),
                media_cache_dir=media_cache_dir,
                health_reporter=health_reporter,
            )
        )
    except WorkerFatalError as exc:
        health_reporter.fatal(exc.detail)
        _log(f"[ERROR] Worker 致命异常: {exc.detail}")
        raise SystemExit(exc.exit_code)
    except Exception as exc:
        detail = f"unexpected:{type(exc).__name__}: {exc}"
        health_reporter.fatal(detail)
        raise
    else:
        health_reporter.stopped("exited")


if __name__ == "__main__":
    main()
