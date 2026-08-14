#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pinduoduo worker, v2 API architecture.

Architecture:
1) ingest: in-page API polling (/plateau/chat/*), no DOM conversation scanning.
2) mailbox: durable inbound queue + per-turn claim/outcome.
3) dispatch: concurrent cross-chat processing (per-chat single-flight).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import random
import sqlite3
import urllib.parse as urllib_parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from playwright.async_api import Browser, Page, Playwright, async_playwright

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

DEFAULT_PORTS = (9223,)
DEFAULT_STORE_ID = os.environ.get("STORE_ID", "pdd_store_example")
DEFAULT_STORE_NAME = os.environ.get("STORE_NAME", "示例拼多多店铺")
DEFAULT_DECISION_URL = os.environ.get("DECISION_URL", "http://127.0.0.1:18080")
DEFAULT_DECISION_KEY = os.environ.get("DECISION_KEY", "")
DEFAULT_DECISION_TIMEOUT_SEC = 75.0
DEFAULT_STATE_DB = str((Path(__file__).resolve().parent / "state" / "pdd_worker.db"))
DEFAULT_FALLBACK_REPLY = "稍等"

DEFAULT_INGEST_POLL_MS = 450
DEFAULT_CONV_PAGE_SIZE = 50
DEFAULT_HISTORY_SIZE = 20
DEFAULT_MAX_DISPATCH_PER_LOOP = 8
DEFAULT_MEDIA_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_PROCESSING_STALE_MS = 3 * 60 * 1000
DEFAULT_ACK_TIMEOUT_SEC = 2.0

PLATFORM = "pdd"
MAX_CHAT_SEND_TEXT = 2000

CHANNEL_CAPABILITIES = {
    "channel": "worker",
    "platform": PLATFORM,
    "send_text": True,
    "send_image": False,
    "send_image_input": "none",
}


@dataclass
class TurnEvent:
    id: int
    message_id: str
    message_type: str
    text: str
    media_url: str
    event_ts_ms: int
    raw_json: str


@dataclass
class PendingTurn:
    turn_id: int
    lock_token: str
    chat_id: str
    customer_id: str
    platform_nickname: str
    session_key: str
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
    text = " ".join(text.split())
    return text[:64]


def _build_customer_id(uid: str) -> str:
    seed = str(uid or "").strip() or "pdd_customer"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _extract_user_uid(conversation: Dict[str, Any]) -> str:
    for side_key in ("to", "from"):
        side = conversation.get(side_key)
        if not isinstance(side, dict):
            continue
        if str(side.get("role") or "").strip() != "user":
            continue
        uid = str(side.get("uid") or "").strip()
        if uid:
            return uid
    return ""


def _extract_user_nickname(conversation: Dict[str, Any]) -> str:
    user_info = conversation.get("user_info")
    if isinstance(user_info, dict):
        nick = _normalize_nickname(str(user_info.get("nickname") or ""))
        if nick:
            return nick
    return ""


def _is_noise_customer_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False

    if value.startswith("[当前用户来自"):
        return True
    if "8-23点间" in value and "客服在线" in value:
        return True
    if "机器人" in value and "接待" in value:
        return True
    return False


def _normalize_customer_inbound_event(
    raw: Dict[str, Any],
    *,
    store_id: str,
    fallback_nickname: str,
    customer_uid: str,
) -> Optional[Dict[str, Any]]:
    from_side = raw.get("from") if isinstance(raw.get("from"), dict) else {}
    if str(from_side.get("role") or "").strip() != "user":
        return None

    uid = str(customer_uid or "").strip()
    if not uid:
        return None

    message_id = str(raw.get("msg_id") or "").strip()
    if not message_id:
        return None

    type_code = _to_int(raw.get("type"), 0)
    content = str(raw.get("content") or "").strip()

    # Explicitly skip platform/system-like side-channel messages.
    if type_code in {24, 31, 41, 56}:
        return None

    message_type = "text"
    text = content
    media_url = ""

    if type_code == 1:
        message_type = "image"
        media_url = content
        text = ""

    if message_type == "text" and _is_noise_customer_text(text):
        return None

    if not text and not media_url:
        return None

    ts_sec = _to_int(raw.get("ts"), 0)
    event_ts_ms = ts_sec * 1000 if ts_sec > 0 else now_ms()

    nickname = _normalize_nickname(fallback_nickname)
    if not nickname:
        nickname = f"user_{uid[-6:]}"

    customer_id = _build_customer_id(uid)

    return {
        "chat_id": uid,
        "customer_id": customer_id,
        "platform_nickname": nickname,
        "session_key": f"{PLATFORM}:{store_id}:{nickname}",
        "message_id": message_id,
        "message_type": message_type,
        "text": text,
        "media_url": media_url,
        "event_ts_ms": event_ts_ms,
        "raw_json": json.dumps(raw, ensure_ascii=False)[:12000],
    }


class PddEventStore:
    """Durable mailbox queue + outbound turns."""

    TBL_CHAT = "pdd_v2_chat_cursor"
    TBL_INBOUND = "pdd_v2_inbound_events"
    TBL_TURN = "pdd_v2_outbound_turns"

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
                    customer_id TEXT NOT NULL DEFAULT '',
                    platform_nickname TEXT NOT NULL DEFAULT '',
                    last_head_msg_id TEXT NOT NULL DEFAULT '',
                    last_customer_msg_id TEXT NOT NULL DEFAULT '',
                    updated_at_ms INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, platform, store_id, chat_id)
                )
                """
            )
            chat_cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({self.TBL_CHAT})")}
            if "last_customer_msg_id" not in chat_cols:
                conn.execute(
                    f"ALTER TABLE {self.TBL_CHAT} ADD COLUMN last_customer_msg_id TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TBL_INBOUND} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    store_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    platform_nickname TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    media_url TEXT NOT NULL DEFAULT '',
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
                ON {self.TBL_INBOUND} (status, available_at_ms, event_ts_ms, id)
                """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.TBL_INBOUND}_processing
                ON {self.TBL_INBOUND} (status, locked_at_ms, id)
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
                    customer_id TEXT NOT NULL,
                    platform_nickname TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    message_type TEXT NOT NULL,
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

    def load_chat_cursors(self, *, tenant_id: str, store_id: str) -> Tuple[Dict[str, str], Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT chat_id, last_head_msg_id, last_customer_msg_id
                FROM {self.TBL_CHAT}
                WHERE tenant_id=? AND platform=? AND store_id=?
                """,
                (tenant_id, PLATFORM, store_id),
            ).fetchall()
        heads: Dict[str, str] = {}
        watermarks: Dict[str, str] = {}
        for row in rows:
            chat_id = str(row["chat_id"] or "").strip()
            if not chat_id:
                continue
            heads[chat_id] = str(row["last_head_msg_id"] or "").strip()
            watermarks[chat_id] = str(row["last_customer_msg_id"] or "").strip()
        return heads, watermarks

    def upsert_chat_head(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        customer_id: str,
        platform_nickname: str,
        head_msg_id: str,
        customer_msg_id: str = "",
    ) -> None:
        now_ts = now_ms()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {self.TBL_CHAT} (
                    tenant_id, platform, store_id, chat_id,
                    customer_id, platform_nickname,
                    last_head_msg_id, last_customer_msg_id, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, platform, store_id, chat_id)
                DO UPDATE SET
                    customer_id=CASE
                        WHEN excluded.customer_id != '' THEN excluded.customer_id
                        ELSE {self.TBL_CHAT}.customer_id
                    END,
                    platform_nickname=CASE
                        WHEN excluded.platform_nickname != '' THEN excluded.platform_nickname
                        ELSE {self.TBL_CHAT}.platform_nickname
                    END,
                    last_head_msg_id=CASE
                        WHEN excluded.last_head_msg_id != '' THEN excluded.last_head_msg_id
                        ELSE {self.TBL_CHAT}.last_head_msg_id
                    END,
                    last_customer_msg_id=CASE
                        WHEN excluded.last_customer_msg_id != '' THEN excluded.last_customer_msg_id
                        ELSE {self.TBL_CHAT}.last_customer_msg_id
                    END,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    tenant_id,
                    PLATFORM,
                    store_id,
                    chat_id,
                    customer_id,
                    platform_nickname,
                    str(head_msg_id or ""),
                    str(customer_msg_id or ""),
                    now_ts,
                ),
            )

    def delete_chat_state_not_in(
        self,
        *,
        tenant_id: str,
        store_id: str,
        visible_chat_ids: Iterable[str],
    ) -> int:
        ids = [str(item or "").strip() for item in visible_chat_ids]
        ids = [item for item in ids if item]
        with self._connect() as conn:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                sql = (
                    f"DELETE FROM {self.TBL_CHAT} "
                    f"WHERE tenant_id=? AND platform=? AND store_id=? "
                    f"AND chat_id NOT IN ({placeholders})"
                )
                args: Tuple[Any, ...] = (tenant_id, PLATFORM, store_id, *ids)
            else:
                sql = (
                    f"DELETE FROM {self.TBL_CHAT} "
                    f"WHERE tenant_id=? AND platform=? AND store_id=?"
                )
                args = (tenant_id, PLATFORM, store_id)
            cur = conn.execute(sql, args)
            return int(cur.rowcount or 0)

    def ingest_events(
        self,
        *,
        tenant_id: str,
        store_id: str,
        events: List[Dict[str, Any]],
    ) -> int:
        if not events:
            return 0
        inserted = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                now_ts = now_ms()
                for event in events:
                    try:
                        conn.execute(
                            f"""
                            INSERT INTO {self.TBL_INBOUND} (
                                tenant_id, platform, store_id,
                                chat_id, customer_id, platform_nickname, session_key,
                                message_id, message_type, text, media_url,
                                event_ts_ms, raw_json,
                                created_at_ms, available_at_ms,
                                updated_at_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                tenant_id,
                                PLATFORM,
                                store_id,
                                event["chat_id"],
                                event["customer_id"],
                                event["platform_nickname"],
                                event["session_key"],
                                event["message_id"],
                                event["message_type"],
                                event["text"],
                                event["media_url"],
                                _to_int(event["event_ts_ms"], now_ts),
                                event["raw_json"],
                                now_ts,
                                now_ts,
                                now_ts,
                            ),
                        )
                        inserted += 1
                    except sqlite3.IntegrityError:
                        continue

                    conn.execute(
                        f"""
                        INSERT INTO {self.TBL_CHAT} (
                            tenant_id, platform, store_id, chat_id,
                            customer_id, platform_nickname,
                            updated_at_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(tenant_id, platform, store_id, chat_id)
                        DO UPDATE SET
                            customer_id=CASE
                                WHEN excluded.customer_id != '' THEN excluded.customer_id
                                ELSE {self.TBL_CHAT}.customer_id
                            END,
                            platform_nickname=CASE
                                WHEN excluded.platform_nickname != '' THEN excluded.platform_nickname
                                ELSE {self.TBL_CHAT}.platform_nickname
                            END,
                            updated_at_ms=excluded.updated_at_ms
                        """,
                        (
                            tenant_id,
                            PLATFORM,
                            store_id,
                            event["chat_id"],
                            event["customer_id"],
                            event["platform_nickname"],
                            now_ts,
                        ),
                    )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return inserted

    def claim_next_turn(
        self,
        *,
        tenant_id: str,
        store_id: str,
        now_ts_ms: int,
        exclude_chat_ids: Optional[Iterable[str]] = None,
    ) -> Optional[PendingTurn]:
        blocked = [str(item or "").strip() for item in (exclude_chat_ids or [])]
        blocked = [item for item in blocked if item]

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale_before_ms = max(0, int(now_ts_ms) - int(DEFAULT_PROCESSING_STALE_MS))
            stale_rows = conn.execute(
                f"""
                SELECT id, turn_id
                FROM {self.TBL_INBOUND}
                WHERE tenant_id=? AND platform=? AND store_id=?
                  AND status='processing'
                  AND locked_at_ms > 0
                  AND locked_at_ms <= ?
                ORDER BY locked_at_ms ASC, id ASC
                LIMIT 200
                """,
                (tenant_id, PLATFORM, store_id, stale_before_ms),
            ).fetchall()
            if stale_rows:
                stale_event_ids = [int(r["id"]) for r in stale_rows]
                stale_turn_ids = [int(r["turn_id"]) for r in stale_rows if _to_int(r["turn_id"], 0) > 0]
                in_placeholders = ",".join("?" for _ in stale_event_ids)
                conn.execute(
                    f"""
                    UPDATE {self.TBL_INBOUND}
                    SET status='new',
                        turn_id=0,
                        lock_token='',
                        locked_at_ms=0,
                        decision_action='',
                        decision_reason='',
                        reply_text='',
                        last_error='',
                        available_at_ms=?,
                        updated_at_ms=?
                    WHERE id IN ({in_placeholders})
                      AND status='processing'
                    """,
                    (now_ts_ms, now_ts_ms, *stale_event_ids),
                )
                if stale_turn_ids:
                    turn_placeholders = ",".join("?" for _ in stale_turn_ids)
                    conn.execute(
                        f"""
                        UPDATE {self.TBL_TURN}
                        SET status='dropped',
                            lock_token='',
                            decision_action='error',
                            decision_reason='stale_reclaimed',
                            last_error='stale_reclaimed',
                            updated_at_ms=?
                        WHERE id IN ({turn_placeholders})
                          AND status='processing'
                        """,
                        (now_ts_ms, *stale_turn_ids),
                    )
                _log(
                    f"[WARN] 回收超时 processing 消息: events={len(stale_event_ids)}, "
                    f"turns={len(stale_turn_ids)}"
                )

            blocked_sql = ""
            params: List[Any] = [tenant_id, PLATFORM, store_id, now_ts_ms]
            if blocked:
                blocked_sql = f" AND e.chat_id NOT IN ({','.join('?' for _ in blocked)})"
                params.extend(blocked)

            row = conn.execute(
                f"""
                SELECT e.*
                FROM {self.TBL_INBOUND} e
                WHERE e.tenant_id=? AND e.platform=? AND e.store_id=?
                  AND e.status='new'
                  AND e.available_at_ms <= ?
                  {blocked_sql}
                ORDER BY e.event_ts_ms ASC, e.id ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()

            if row is None:
                conn.commit()
                return None

            event_id = int(row["id"])
            chat_id = str(row["chat_id"] or "")
            lock_token = hashlib.sha1(
                f"pdd_turn:{chat_id}:{event_id}:{now_ts_ms}".encode("utf-8")
            ).hexdigest()[:20]

            turn_cur = conn.execute(
                f"""
                INSERT INTO {self.TBL_TURN} (
                    tenant_id, platform, store_id,
                    chat_id, customer_id, platform_nickname, session_key,
                    message_id, message_type, request_text,
                    status, attempts, lock_token,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'processing', 1, ?, ?, ?)
                """,
                (
                    tenant_id,
                    PLATFORM,
                    store_id,
                    chat_id,
                    str(row["customer_id"] or ""),
                    str(row["platform_nickname"] or ""),
                    str(row["session_key"] or ""),
                    str(row["message_id"] or ""),
                    str(row["message_type"] or "text"),
                    str(row["text"] or ""),
                    lock_token,
                    now_ts_ms,
                    now_ts_ms,
                ),
            )
            turn_id = int(turn_cur.lastrowid)

            updated = conn.execute(
                f"""
                UPDATE {self.TBL_INBOUND}
                SET status='processing',
                    attempts=attempts+1,
                    turn_id=?,
                    lock_token=?,
                    locked_at_ms=?,
                    updated_at_ms=?
                WHERE id=?
                  AND tenant_id=? AND platform=? AND store_id=?
                  AND status='new'
                """,
                (
                    turn_id,
                    lock_token,
                    now_ts_ms,
                    now_ts_ms,
                    event_id,
                    tenant_id,
                    PLATFORM,
                    store_id,
                ),
            ).rowcount

            if updated != 1:
                conn.rollback()
                return None

            conn.commit()

        event = TurnEvent(
            id=event_id,
            message_id=str(row["message_id"] or ""),
            message_type=str(row["message_type"] or "text"),
            text=str(row["text"] or ""),
            media_url=str(row["media_url"] or ""),
            event_ts_ms=_to_int(row["event_ts_ms"], 0),
            raw_json=str(row["raw_json"] or "{}"),
        )

        return PendingTurn(
            turn_id=turn_id,
            lock_token=lock_token,
            chat_id=chat_id,
            customer_id=str(row["customer_id"] or ""),
            platform_nickname=str(row["platform_nickname"] or ""),
            session_key=str(row["session_key"] or ""),
            events=[event],
        )

    def mark_turn_outcome(
        self,
        *,
        tenant_id: str,
        store_id: str,
        chat_id: str,
        customer_id: str,
        platform_nickname: str,
        turn_id: int,
        lock_token: str,
        event_ids: List[int],
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

            conn.execute(
                f"""
                INSERT INTO {self.TBL_CHAT} (
                    tenant_id, platform, store_id, chat_id,
                    customer_id, platform_nickname,
                    updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, platform, store_id, chat_id)
                DO UPDATE SET
                    customer_id=CASE
                        WHEN excluded.customer_id != '' THEN excluded.customer_id
                        ELSE {self.TBL_CHAT}.customer_id
                    END,
                    platform_nickname=CASE
                        WHEN excluded.platform_nickname != '' THEN excluded.platform_nickname
                        ELSE {self.TBL_CHAT}.platform_nickname
                    END,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    tenant_id,
                    PLATFORM,
                    store_id,
                    chat_id,
                    customer_id,
                    platform_nickname,
                    now_ts,
                ),
            )

            conn.commit()


API_POST_SCRIPT = r"""
async (args) => {
    const path = String(args.path || '');
    const data = args.data && typeof args.data === 'object' ? JSON.parse(JSON.stringify(args.data)) : {};

    let anti = '';
    try {
        if (window.riskControlCrawler && typeof window.riskControlCrawler.messagePack === 'function') {
            const now = Date.now();
            const diff = Number(window.SERVER_DIFF_TIME || 0);
            if (typeof window.riskControlCrawler.updateServerTime === 'function') {
                window.riskControlCrawler.updateServerTime(diff + now);
            }
            anti = String(window.riskControlCrawler.messagePack() || '').trim();
        }
    } catch (e) {
        anti = '';
    }

    const headers = {
        'content-type': 'application/json',
        'accept': 'application/json, text/plain, */*',
    };
    if (anti) {
        headers['anti-content'] = anti;
    }

    if (anti) {
        if (typeof data === 'object' && data !== null && !data.anti_content) {
            data.anti_content = anti;
        }
        if (typeof data.data === 'object' && data.data !== null && !data.data.anti_content) {
            data.data.anti_content = anti;
        }
    }

    const url = path.startsWith('/') ? path : `/${path}`;
    try {
        const resp = await fetch(url, {
            method: 'POST',
            credentials: 'include',
            headers,
            body: JSON.stringify(data),
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


class PddApiClient:
    def __init__(self) -> None:
        self._rid_seed = 0

    def _next_request_id(self) -> int:
        self._rid_seed = (self._rid_seed + 1) % 1000
        return int(f"{now_ms() % 100000000:08d}") + self._rid_seed

    async def post(
        self,
        page: Page,
        *,
        path: str,
        data: Dict[str, Any],
        api_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, Any]:
        async def _send() -> Dict[str, Any]:
            out = await page.evaluate(API_POST_SCRIPT, {"path": path, "data": data})
            text = str((out or {}).get("text") or "")
            parsed: Dict[str, Any] = {}
            if text:
                try:
                    obj = json.loads(text)
                    if isinstance(obj, dict):
                        parsed = obj
                    else:
                        parsed = {"result": obj}
                except Exception:
                    parsed = {"raw_text": text[:2000]}
            parsed.setdefault("_http_ok", bool((out or {}).get("ok")))
            parsed.setdefault("_http_status", _to_int((out or {}).get("status"), 0))
            if (out or {}).get("error") and "error_msg" not in parsed:
                parsed["error_msg"] = str((out or {}).get("error"))
            return parsed

        if api_lock is None:
            return await _send()
        async with api_lock:
            return await _send()

    async def get_latest_conversations(
        self,
        page: Page,
        *,
        page_no: int,
        size: int,
        api_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, Any]:
        payload = {
            "data": {
                "request_id": self._next_request_id(),
                "cmd": "latest_conversations",
                "version": 2,
                "need_unreply_time": True,
                "page": max(1, int(page_no)),
                "size": max(1, int(size)),
            },
            "client": "WEB",
        }
        return await self.post(page, path="/plateau/chat/latest_conversations", data=payload, api_lock=api_lock)

    async def get_history_messages(
        self,
        page: Page,
        *,
        uid: str,
        size: int,
        chat_type: str = "",
        api_lock: Optional[asyncio.Lock] = None,
    ) -> Dict[str, Any]:
        list_payload: Dict[str, Any] = {
            "with": {
                "role": "user",
                "id": str(uid),
            },
            "start_msg_id": None,
            "start_index": 0,
            "size": max(1, int(size)),
        }
        if chat_type:
            list_payload["chat_type"] = chat_type

        payload = {
            "data": {
                "cmd": "list",
                "request_id": self._next_request_id(),
                "list": list_payload,
                "notUpdateUnreplyTs": True,
            },
            "client": "WEB",
        }
        return await self.post(page, path="/plateau/chat/list", data=payload, api_lock=api_lock)

    async def send_text_message(
        self,
        page: Page,
        *,
        uid: str,
        text: str,
        chat_type: str = "",
        api_lock: Optional[asyncio.Lock] = None,
    ) -> Tuple[bool, str, str]:
        clean_text = str(text or "").strip()
        if not clean_text:
            return False, "empty_text", ""

        async def _send_once(attempt: int) -> Tuple[bool, str, str]:
            request_id = self._next_request_id()
            now_sec = max(1, now_ms() // 1000)
            msg_hash = hashlib.sha1(
                f"{uid}|{request_id}|{clean_text}".encode("utf-8")
            ).hexdigest()[:16]

            message: Dict[str, Any] = {
                "to": {"role": "user", "uid": str(uid)},
                "from": {"role": "mall_cs"},
                "ts": int(now_sec),
                "content": clean_text[:MAX_CHAT_SEND_TEXT],
                "msg_id": 9007199254740991,
                "type": 0,
                "is_aut": 0,
                "manual_reply": 1,
                "status": "read",
                "is_read": 1,
                "hash": msg_hash,
            }
            if chat_type:
                message["chat_type"] = chat_type

            payload = {
                "data": {
                    "cmd": "send_message",
                    "request_id": request_id,
                    "message": message,
                    "random": f"r{request_id}{random.randint(1000, 9999)}",
                },
                "client": "WEB",
            }

            resp = await self.post(page, path="/plateau/chat/send_message", data=payload, api_lock=api_lock)
            result = resp.get("result") if isinstance(resp.get("result"), dict) else {}

            ok = bool(resp.get("success") is True and str(result.get("result") or "") == "ok")
            if ok:
                return True, "", str(result.get("msg_id") or "")

            reason = (
                str(result.get("reason") or "").strip()
                or str(resp.get("error_msg") or "").strip()
                or f"http_{_to_int(resp.get('_http_status'), 0)}"
            )
            diag_obj = {
                "attempt": int(attempt),
                "http_ok": bool(resp.get("_http_ok")),
                "http_status": _to_int(resp.get("_http_status"), 0),
                "success": resp.get("success"),
                "error_msg": str(resp.get("error_msg") or "")[:220],
                "result_result": str(result.get("result") or "")[:80],
                "result_reason": str(result.get("reason") or "")[:220],
                "result_msg_id": str(result.get("msg_id") or "")[:80],
                "raw_text": str(resp.get("raw_text") or "")[:300],
            }
            try:
                diag_text = json.dumps(diag_obj, ensure_ascii=False)
            except Exception:
                diag_text = str(diag_obj)
            _log(
                f"[WARN] pdd_send_diag uid={uid}, request_id={request_id}, reason={reason}, "
                f"resp={diag_text[:900]}"
            )
            return False, reason, ""

        first_ok, first_reason, first_msg_id = await _send_once(1)
        if first_ok:
            return True, "", first_msg_id

        await asyncio.sleep(0.8)
        _log(f"[WARN] pdd_send_retry uid={uid}, reason={first_reason or 'send_failed'}")

        second_ok, second_reason, second_msg_id = await _send_once(2)
        if second_ok:
            _log(f"[INFO] pdd_send_retry_ok uid={uid}, msg_id={second_msg_id}")
            return True, "", second_msg_id

        return False, second_reason or first_reason or "send_failed", ""


def _api_success(resp: Dict[str, Any]) -> bool:
    if not isinstance(resp, dict):
        return False
    if resp.get("success") is True:
        return True
    # Some endpoints return {result: {...}} without explicit success.
    return isinstance(resp.get("result"), dict)


def _api_result_dict(resp: Dict[str, Any]) -> Dict[str, Any]:
    result = resp.get("result")
    return result if isinstance(result, dict) else {}


def _safe_json_dict(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


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


def _schedule_ack_delivery(
    decision_client: DecisionClient,
    *,
    msg: IncomingMessage,
    decision: Any,
    sent_text: str,
    sent_image_urls: List[str],
    status: str,
) -> None:
    session_key = msg.session_key()

    async def _run() -> None:
        try:
            ok = await asyncio.wait_for(
                decision_client.ack_delivery(
                    msg=msg,
                    decision=decision,
                    sent_text=sent_text,
                    sent_image_urls=sent_image_urls,
                    status=status,
                ),
                timeout=DEFAULT_ACK_TIMEOUT_SEC,
            )
            if not ok:
                _log(f"[WARN][{session_key}] ack_delivery failed.")
        except asyncio.TimeoutError:
            _log(f"[WARN][{session_key}] ack_delivery timeout({DEFAULT_ACK_TIMEOUT_SEC}s).")
        except Exception as exc:
            _log(f"[WARN][{session_key}] ack_delivery exception: {type(exc).__name__}: {exc}")

    try:
        asyncio.create_task(_run())
    except Exception as exc:
        _log(f"[WARN][{session_key}] ack_delivery schedule failed: {type(exc).__name__}: {exc}")


async def _ingest_once(
    page: Page,
    api_client: PddApiClient,
    event_store: PddEventStore,
    *,
    tenant_id: str,
    store_id: str,
    chat_head_cache: Dict[str, str],
    chat_customer_watermark_cache: Dict[str, str],
    conv_page_size: int,
    history_size: int,
    api_lock: Optional[asyncio.Lock] = None,
) -> Tuple[int, int, int]:
    latest = await api_client.get_latest_conversations(
        page,
        page_no=1,
        size=conv_page_size,
        api_lock=api_lock,
    )
    if not _api_success(latest):
        raise RuntimeError(f"latest_conversations_failed:{latest}")

    result = _api_result_dict(latest)
    if str(result.get("result") or "").strip().lower() not in {"", "ok"}:
        raise RuntimeError(f"latest_conversations_ret:{result.get('result')}")

    conversations = list(result.get("conversations") or result.get("data") or [])

    inserted = 0
    scanned = 0
    fetched = 0
    visible_chat_ids: set[str] = set()

    for conversation in conversations:
        scanned += 1
        uid = _extract_user_uid(conversation)
        if not uid:
            continue
        visible_chat_ids.add(uid)

        head_msg_id = str(conversation.get("msg_id") or "").strip()
        nickname = _extract_user_nickname(conversation)
        if not nickname:
            nickname = f"user_{uid[-6:]}"
        customer_id = _build_customer_id(uid)
        prev_head = str(chat_head_cache.get(uid) or "").strip()
        prev_customer_msg_id = str(chat_customer_watermark_cache.get(uid) or "").strip()

        event_store.upsert_chat_head(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=uid,
            customer_id=customer_id,
            platform_nickname=nickname,
            head_msg_id=head_msg_id,
            customer_msg_id=prev_customer_msg_id,
        )

        if head_msg_id and prev_head and head_msg_id == prev_head:
            continue

        fetched += 1
        chat_type = str(conversation.get("chat_type") or "").strip()
        history = await api_client.get_history_messages(
            page,
            uid=uid,
            size=history_size,
            chat_type=chat_type,
            api_lock=api_lock,
        )

        if not _api_success(history):
            _log(f"[WARN] 拉取会话消息失败 uid={uid}, resp={history}")
            continue

        history_result = _api_result_dict(history)
        if str(history_result.get("result") or "").strip().lower() not in {"", "ok"}:
            _log(
                f"[WARN] 拉取会话消息返回异常 uid={uid}, result={history_result.get('result')}"
            )
            continue

        rows = list(history_result.get("messages") or [])
        if not rows:
            chat_head_cache[uid] = head_msg_id or prev_head
            continue

        rows.sort(
            key=lambda m: (
                _to_int(m.get("ts"), 0),
                str(m.get("msg_id") or ""),
            )
        )

        payloads: List[Dict[str, Any]] = []
        for raw in rows:
            event_payload = _normalize_customer_inbound_event(
                raw,
                store_id=store_id,
                fallback_nickname=nickname,
                customer_uid=uid,
            )
            if event_payload is None:
                continue
            payloads.append(event_payload)

        if payloads:
            if not prev_head:
                selected_payloads = [payloads[-1]]
            else:
                start_idx = -1
                if prev_customer_msg_id:
                    for i, payload in enumerate(payloads):
                        if str(payload.get("message_id") or "") == prev_customer_msg_id:
                            start_idx = i
                if start_idx >= 0:
                    selected_payloads = payloads[start_idx + 1 :]
                else:
                    selected_payloads = [payloads[-1]]

            inserted += event_store.ingest_events(
                tenant_id=tenant_id,
                store_id=store_id,
                events=selected_payloads,
            )
            latest_customer_msg_id = str(payloads[-1].get("message_id") or "")
            if latest_customer_msg_id:
                chat_customer_watermark_cache[uid] = latest_customer_msg_id
                event_store.upsert_chat_head(
                    tenant_id=tenant_id,
                    store_id=store_id,
                    chat_id=uid,
                    customer_id=customer_id,
                    platform_nickname=nickname,
                    head_msg_id=head_msg_id,
                    customer_msg_id=latest_customer_msg_id,
                )

        chat_head_cache[uid] = head_msg_id or prev_head

    deleted = event_store.delete_chat_state_not_in(
        tenant_id=tenant_id,
        store_id=store_id,
        visible_chat_ids=visible_chat_ids,
    )
    if deleted > 0:
        _log(f"[INFO] 会话状态清理：删除 {deleted} 条已不在当前列表的水位。")
    stale_ids = [chat_id for chat_id in chat_head_cache.keys() if chat_id not in visible_chat_ids]
    for chat_id in stale_ids:
        chat_head_cache.pop(chat_id, None)
        chat_customer_watermark_cache.pop(chat_id, None)

    return inserted, scanned, fetched


async def _process_claimed_turn(
    turn: PendingTurn,
    page: Page,
    decision_client: DecisionClient,
    api_client: PddApiClient,
    event_store: PddEventStore,
    state_store: LocalStateStore,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    media_cache_dir: Path,
    api_lock: Optional[asyncio.Lock] = None,
) -> bool:
    event = turn.events[-1]
    raw_obj = _safe_json_dict(event.raw_json)
    chat_type = str(raw_obj.get("chat_type") or "").strip()
    turn_media_url = str(event.media_url or "").strip()
    raw_media_url = turn_media_url

    if event.message_type == "image":
        if not turn_media_url:
            event_store.mark_turn_outcome(
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=turn.chat_id,
                customer_id=turn.customer_id,
                platform_nickname=turn.platform_nickname,
                turn_id=turn.turn_id,
                lock_token=turn.lock_token,
                event_ids=turn.event_ids,
                status="dropped",
                decision_action="drop",
                decision_reason="image_media_url_empty",
                reply_text="",
                last_error="image_media_url_empty",
            )
            _log(f"[WARN][{turn.session_key}] 图片回合无可用链接，已丢弃。")
            return True

        local_media_url = await _materialize_media_url_to_local(
            page,
            turn_media_url,
            media_cache_dir=media_cache_dir,
        )
        if not local_media_url:
            event_store.mark_turn_outcome(
                tenant_id=tenant_id,
                store_id=store_id,
                chat_id=turn.chat_id,
                customer_id=turn.customer_id,
                platform_nickname=turn.platform_nickname,
                turn_id=turn.turn_id,
                lock_token=turn.lock_token,
                event_ids=turn.event_ids,
                status="dropped",
                decision_action="drop",
                decision_reason="image_media_localize_failed",
                reply_text="",
                last_error="image_media_localize_failed",
            )
            _log(f"[WARN][{turn.session_key}] 图片本地化失败，已丢弃该回合。")
            return True

        turn_media_url = local_media_url
        _log(f"[INFO][{turn.session_key}] 图片本地化成功: {turn_media_url[:120]}")

    incoming = IncomingMessage(
        tenant_id=tenant_id,
        platform=PLATFORM,
        store_id=store_id,
        store_name=store_name or store_id,
        customer_id=turn.customer_id,
        platform_nickname=turn.platform_nickname,
        message_id=event.message_id,
        message_type=event.message_type,
        text=event.text,
        media_url=turn_media_url,
        raw={
            "page_url": page.url,
            "channel_capabilities": dict(CHANNEL_CAPABILITIES),
            "chat_id": turn.chat_id,
            "turn_id": turn.turn_id,
            "turn_event_ids": turn.event_ids,
            "chat_type": chat_type,
            "media_url_raw": raw_media_url,
        },
        role="customer",
    )

    _log(
        f"[INFO][{incoming.session_key()}] 出队回合 turn_id={turn.turn_id}, "
        f"message_id={incoming.message_id}, type={incoming.message_type}"
    )

    mode = state_store.get_mode(incoming)
    if mode != "auto":
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            customer_id=turn.customer_id,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            status="dropped",
            decision_action="manual",
            decision_reason="mode_manual",
            reply_text="",
            last_error="",
        )
        _log(f"[INFO][{incoming.session_key()}] 当前为 {mode} 模式，回合丢弃。")
        return True

    try:
        decision = await decision_client.decide(incoming)
    except Exception as exc:
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            customer_id=turn.customer_id,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            status="dropped",
            decision_action="error",
            decision_reason="decision_exception",
            reply_text="",
            last_error=f"decision_exception:{type(exc).__name__}",
        )
        _log(f"[WARN][{incoming.session_key()}] 决策异常: {type(exc).__name__}: {exc}")
        return True

    action = str(decision.action or "").strip().lower()
    reason = str(decision.reason or "").strip()
    _log(f"[INFO][{incoming.session_key()}] decision action={action}, reason={reason}")

    if action == "manual":
        state_store.set_mode(incoming, "manual", now_ms())
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            customer_id=turn.customer_id,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            status="dropped",
            decision_action="manual",
            decision_reason=reason or "manual_mode",
            reply_text="",
            last_error="",
        )
        return True

    if action != "send":
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            customer_id=turn.customer_id,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            status="dropped",
            decision_action=action or "skip",
            decision_reason=reason or "decision_skip",
            reply_text="",
            last_error="",
        )
        return True

    reply_text = str(decision.reply_text or "").strip()
    if not reply_text:
        drop_reason = "empty_reply_text"
        if decision.reply_image_urls:
            drop_reason = "image_only_not_supported"

        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            customer_id=turn.customer_id,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            status="dropped",
            decision_action="send",
            decision_reason=f"{reason}:{drop_reason}".strip(":"),
            reply_text="",
            last_error=drop_reason,
        )
        _schedule_ack_delivery(
            decision_client,
            msg=incoming,
            decision=decision,
            sent_text="",
            sent_image_urls=[],
            status="failed",
        )
        _log(f"[WARN][{incoming.session_key()}] 无可发送文本，回合丢弃。reason={drop_reason}")
        return True

    sent_ok, send_error, sent_msg_id = await api_client.send_text_message(
        page,
        uid=turn.chat_id,
        text=reply_text,
        chat_type=chat_type,
        api_lock=api_lock,
    )

    if sent_ok:
        state_store.mark_reply(incoming, now_ms(), reply_text=reply_text)
        event_store.mark_turn_outcome(
            tenant_id=tenant_id,
            store_id=store_id,
            chat_id=turn.chat_id,
            customer_id=turn.customer_id,
            platform_nickname=turn.platform_nickname,
            turn_id=turn.turn_id,
            lock_token=turn.lock_token,
            event_ids=turn.event_ids,
            status="replied",
            decision_action="send",
            decision_reason=reason or "sent",
            reply_text=reply_text,
            last_error="",
        )
        _schedule_ack_delivery(
            decision_client,
            msg=incoming,
            decision=decision,
            sent_text=reply_text,
            sent_image_urls=[],
            status="sent",
        )
        _log(f"[INFO][{incoming.session_key()}] 已发送回复 msg_id={sent_msg_id}")
        return True

    event_store.mark_turn_outcome(
        tenant_id=tenant_id,
        store_id=store_id,
        chat_id=turn.chat_id,
        customer_id=turn.customer_id,
        platform_nickname=turn.platform_nickname,
        turn_id=turn.turn_id,
        lock_token=turn.lock_token,
        event_ids=turn.event_ids,
        status="dropped",
        decision_action="send",
        decision_reason=f"{reason}:send_failed".strip(":"),
        reply_text="",
        last_error=send_error or "send_failed",
    )
    _schedule_ack_delivery(
        decision_client,
        msg=incoming,
        decision=decision,
        sent_text="",
        sent_image_urls=[],
        status="failed",
    )
    _log(f"[WARN][{incoming.session_key()}] 发送失败，回合丢弃。reason={send_error or 'send_failed'}")
    return True


async def _process_claimed_turn_safe(
    turn: PendingTurn,
    page: Page,
    decision_client: DecisionClient,
    api_client: PddApiClient,
    event_store: PddEventStore,
    state_store: LocalStateStore,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
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
                customer_id=turn.customer_id,
                platform_nickname=turn.platform_nickname,
                turn_id=turn.turn_id,
                lock_token=turn.lock_token,
                event_ids=turn.event_ids,
                status="dropped",
                decision_action="error",
                decision_reason="turn_process_exception",
                reply_text="",
                last_error=err,
            )
        except Exception as mark_exc:
            _log(
                f"[ERROR][{turn.session_key}] 回写失败: {type(mark_exc).__name__}: {mark_exc}"
            )
        return True


async def _ingest_loop(
    page: Page,
    api_client: PddApiClient,
    event_store: PddEventStore,
    health_reporter: WorkerHealthReporter,
    *,
    tenant_id: str,
    store_id: str,
    conv_page_size: int,
    history_size: int,
    ingest_poll_ms: int,
    api_lock: asyncio.Lock,
) -> None:
    idle_sleep_sec = max(0.12, int(ingest_poll_ms) / 1000.0)
    chat_head_cache, chat_customer_watermark_cache = event_store.load_chat_cursors(
        tenant_id=tenant_id,
        store_id=store_id,
    )

    while True:
        inserted = 0
        scanned = 0
        fetched = 0
        try:
            inserted, scanned, fetched = await _ingest_once(
                page,
                api_client,
                event_store,
                tenant_id=tenant_id,
                store_id=store_id,
                chat_head_cache=chat_head_cache,
                chat_customer_watermark_cache=chat_customer_watermark_cache,
                conv_page_size=conv_page_size,
                history_size=history_size,
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
            _log(f"[INFO] API 采集完成：扫描会话 {scanned} 个，拉取 {fetched} 个，入队 {inserted} 条。")

        await asyncio.sleep(0.06 if inserted > 0 else idle_sleep_sec)


async def _dispatch_loop(
    page: Page,
    decision_client: DecisionClient,
    api_client: PddApiClient,
    event_store: PddEventStore,
    state_store: LocalStateStore,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    media_cache_dir: Path,
    max_dispatch_per_loop: int,
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


def _score_page_url(url: str) -> int:
    if not url:
        return 0
    if "mms.pinduoduo.com" in url and "chat-merchant" in url:
        return 120
    if "mms.pinduoduo.com" in url:
        return 100
    if "pinduoduo.com" in url:
        return 20
    return 0


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
                    url = (p.url or "").strip()
                    if url:
                        urls.append(url)
            if urls:
                preview = " | ".join(urls[:5])
                _log(f"[WARN] 端口 {port} 已连接，但未找到拼多多客服页。当前标签页: {preview}")
            else:
                _log(f"[WARN] 端口 {port} 已连接，但浏览器无可用标签页。")
            _disconnect_browser(browser)
            continue

        _log(f"[INFO] 端口 {port} 绑定页面: {page.url}")
        pairs.append((browser, page))
    return pairs


async def monitor_single_browser(
    page: Page,
    decision_client: DecisionClient,
    state_store: LocalStateStore,
    event_store: PddEventStore,
    api_client: PddApiClient,
    health_reporter: WorkerHealthReporter,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    media_cache_dir: Path,
    ingest_poll_ms: int,
    conv_page_size: int,
    history_size: int,
    max_dispatch_per_loop: int,
) -> None:
    api_lock = asyncio.Lock()

    try:
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(800)
        health_reporter.ready("page_attached")

        ingest_task = asyncio.create_task(
            _ingest_loop(
                page,
                api_client,
                event_store,
                health_reporter,
                tenant_id=tenant_id,
                store_id=store_id,
                conv_page_size=conv_page_size,
                history_size=history_size,
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
                media_cache_dir=media_cache_dir,
                max_dispatch_per_loop=max_dispatch_per_loop,
                ingest_poll_ms=ingest_poll_ms,
                api_lock=api_lock,
            )
        )

        try:
            await asyncio.gather(ingest_task, dispatch_task)
        finally:
            for task in (ingest_task, dispatch_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ingest_task, dispatch_task, return_exceptions=True)

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
    event_store: PddEventStore,
    api_client: PddApiClient,
    health_reporter: WorkerHealthReporter,
    *,
    tenant_id: str,
    store_id: str,
    store_name: str,
    media_cache_dir: Path,
    ingest_poll_ms: int,
    conv_page_size: int,
    history_size: int,
    max_dispatch_per_loop: int,
) -> None:
    _log(f"[INFO] Worker 启动，待连接端口: {list(ports)}")
    async with async_playwright() as playwright:
        while True:
            browser_page_pairs = await _collect_browsers(playwright, ports)
            if not browser_page_pairs:
                health_reporter.degraded("page_not_found")
                _log("[WARN] 未找到拼多多客服页，3秒后重试。")
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
                            health_reporter=health_reporter,
                            tenant_id=tenant_id,
                            store_id=store_id,
                            store_name=store_name,
                            media_cache_dir=media_cache_dir,
                            ingest_poll_ms=ingest_poll_ms,
                            conv_page_size=conv_page_size,
                            history_size=history_size,
                            max_dispatch_per_loop=max_dispatch_per_loop,
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
    parser = argparse.ArgumentParser(description="拼多多客服 Worker（API 队列版）")
    parser.add_argument(
        "--ports",
        type=int,
        nargs="+",
        default=list(DEFAULT_PORTS),
        help="Chrome 远程调试端口，默认 9223",
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
        help="决策服务不可用时的兜底回复文本",
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
        "--ingest-poll-ms",
        type=int,
        default=DEFAULT_INGEST_POLL_MS,
        help="无任务时 ingest 轮询间隔（毫秒）",
    )
    parser.add_argument(
        "--conv-page-size",
        type=int,
        default=DEFAULT_CONV_PAGE_SIZE,
        help="每轮 latest_conversations 拉取数量",
    )
    parser.add_argument(
        "--history-size",
        type=int,
        default=DEFAULT_HISTORY_SIZE,
        help="每会话单次 list 拉取条数",
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
        f"ingest_poll_ms={args.ingest_poll_ms}, conv_page_size={args.conv_page_size}, "
        f"history_size={args.history_size}, max_dispatch_per_loop={args.max_dispatch_per_loop}"
    )

    decision_client = DecisionClient(
        base_url=args.decision_url,
        api_key=args.decision_key,
        timeout_sec=args.decision_timeout_sec,
        fallback_text=args.fallback_text,
    )
    state_store = LocalStateStore(args.state_db)
    event_store = PddEventStore(args.state_db)
    api_client = PddApiClient()
    health_reporter = WorkerHealthReporter.from_env(
        worker_key=f"{PLATFORM}_{args.store_id}",
        worker_kind=PLATFORM,
        store_id=args.store_id,
        store_name=args.store_name or args.store_id,
    )
    media_cache_dir = Path(args.state_db).resolve().parent / "media" / "pdd" / args.store_id
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
                health_reporter=health_reporter,
                tenant_id=args.tenant_id,
                store_id=args.store_id,
                store_name=args.store_name,
                media_cache_dir=media_cache_dir,
                ingest_poll_ms=max(80, int(args.ingest_poll_ms)),
                conv_page_size=max(10, min(200, int(args.conv_page_size))),
                history_size=max(5, min(100, int(args.history_size))),
                max_dispatch_per_loop=max(1, int(args.max_dispatch_per_loop)),
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
